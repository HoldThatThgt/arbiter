package interpose

import (
	"os"
	"os/exec"
	"sort"
	"strconv"
	"testing"
	"time"
)

const (
	// startupBudgetEnv overrides the computed budget with an absolute,
	// positive integer number of milliseconds, for hosts whose spawn cost
	// the reference calibration does not model well.
	startupBudgetEnv = "ARBITER_CC_P95_BUDGET_MS"

	// defaultStartupBudget is the spec budget (docs/modules/go-interpose.md,
	// issue #24): on reference hardware `arbiter cc` startup p95 <= 3ms.
	defaultStartupBudget = 3 * time.Millisecond

	// referenceSpawnCost is the bare process-spawn cost (exec of `true`)
	// backing the 3ms spec number. Virtualized CI hosts spawn processes
	// several times slower than that; the budget scales with the measured
	// ratio so the gate keeps its strictness on reference hardware without
	// turning into pure host-speed noise elsewhere. The scale never shrinks
	// the budget below the spec value.
	referenceSpawnCost = 300 * time.Microsecond

	startupWarmups = 5
	startupSamples = 100
)

// TestArbiterCCStartupP95 gates `arbiter cc` startup cost: the shim sits on
// the hot path of every compiler invocation, so a regression that adds heavy
// init (config parsing, engine spawn, ...) must fail CI.
//
// The measured binary is the separately `go build`-built arbiter from
// buildArbiter (adversarial_test.go), never the race-instrumented test
// binary. The invocation is the cheapest valid classification path: a
// non-compile passthrough (`arbiter cc -- true`), which journals nothing.
// Execs are sequential and single-threaded; warm-up runs (page cache,
// dynamic loader) are excluded from the measured distribution.
func TestArbiterCCStartupP95(t *testing.T) {
	bin := buildArbiter(t)
	truePath, err := exec.LookPath("true")
	if err != nil {
		t.Fatalf("no `true` binary on PATH: %v", err)
	}
	work := t.TempDir()

	// Baseline and measured execs are interleaved sample-by-sample so that
	// fluctuating host load (e.g. the rest of the test suite running in
	// parallel packages) inflates both distributions equally instead of
	// just the measured one; the budget then scales tail-to-tail.
	baselines, durations := measureInterleaved(t, work, []string{truePath}, []string{bin, "cc", "--", truePath})
	baseline := baselines[percentileIndex(startupSamples, 95)]

	p50 := durations[percentileIndex(startupSamples, 50)]
	p95 := durations[percentileIndex(startupSamples, 95)]
	max := durations[startupSamples-1]
	budget := startupBudget(t, baseline)
	if p95 > budget {
		t.Fatalf("arbiter cc startup p95 = %v exceeds budget %v (n=%d p50=%v p95=%v max=%v; host spawn baseline=%v; override with %s)",
			p95, budget, startupSamples, p50, p95, max, baseline, startupBudgetEnv)
	}
	t.Logf("arbiter cc startup: n=%d p50=%v p95=%v max=%v (budget %v, host spawn baseline %v)",
		startupSamples, p50, p95, max, budget, baseline)
}

// measureInterleaved runs the baseline and measured argvs back-to-back within
// each iteration (after shared warm-ups) and returns both sorted post-warm-up
// wall-clock distributions, so transient load skews both sides alike.
func measureInterleaved(t *testing.T, dir string, baselineArgv, measuredArgv []string) (baselines, durations []time.Duration) {
	t.Helper()
	run := func(argv []string) time.Duration {
		cmd := exec.Command(argv[0], argv[1:]...)
		cmd.Dir = dir
		start := time.Now()
		err := cmd.Run()
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("%v failed: %v", argv, err)
		}
		return elapsed
	}
	for i := 0; i < startupWarmups; i++ {
		run(baselineArgv)
		run(measuredArgv)
	}
	baselines = make([]time.Duration, 0, startupSamples)
	durations = make([]time.Duration, 0, startupSamples)
	for i := 0; i < startupSamples; i++ {
		baselines = append(baselines, run(baselineArgv))
		durations = append(durations, run(measuredArgv))
	}
	sort.Slice(baselines, func(i, j int) bool { return baselines[i] < baselines[j] })
	sort.Slice(durations, func(i, j int) bool { return durations[i] < durations[j] })
	return baselines, durations
}

// startupBudget resolves the p95 budget. ARBITER_CC_P95_BUDGET_MS, when set,
// is absolute and wins; an unparsable override fails the test rather than
// silently mismeasuring. Otherwise the spec's 3ms is scaled by how much
// slower this host spawns a trivial process than the reference host.
func startupBudget(t *testing.T, baseline time.Duration) time.Duration {
	t.Helper()
	if raw := os.Getenv(startupBudgetEnv); raw != "" {
		ms, err := strconv.Atoi(raw)
		if err != nil || ms <= 0 {
			t.Fatalf("%s = %q is not a positive integer", startupBudgetEnv, raw)
		}
		return time.Duration(ms) * time.Millisecond
	}
	budget := defaultStartupBudget
	if baseline > referenceSpawnCost {
		budget = time.Duration(int64(defaultStartupBudget) * int64(baseline) / int64(referenceSpawnCost))
	}
	return budget
}

// percentileIndex returns the nearest-rank index for percentile p over n
// sorted samples: ceil(p/100 * n) - 1.
func percentileIndex(n, p int) int {
	idx := (p*n + 99) / 100
	if idx < 1 {
		idx = 1
	}
	if idx > n {
		idx = n
	}
	return idx - 1
}
