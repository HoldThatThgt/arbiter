package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/HoldThatThgt/arbiter/internal/cli"
	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/interpose"
	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/seat"
)

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "cc" {
		root, err := os.Getwd()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(interpose.Run(root, os.Args[2:], os.Stdin, os.Stdout, os.Stderr))
	}
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: arbiter init [flags] | adopt | status [--json] | report [--json] [match_id] | serve <seat> | hook stop | cc -- <real-compiler> [args...]")
	}
	root, err := os.Getwd()
	if err != nil {
		return err
	}
	switch os.Args[1] {
	case "help", "-h", "--help":
		fmt.Print(usage)
		return nil
	case "init":
		if wantsHelp(os.Args[2:]) {
			fmt.Print(initHelp)
			return nil
		}
		fs := flag.NewFlagSet("init", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		opts := deploy.Options{}
		fs.BoolVar(&opts.NoExecutor, "no-executor", false, "skip executor agent")
		fs.BoolVar(&opts.Remove, "remove", false, "remove generated init wiring")
		fs.BoolVar(&opts.EmbeddedEngine, "embedded-engine", false, "deny edits to embedded engine files")
		if err := fs.Parse(os.Args[2:]); err != nil || fs.NArg() != 0 {
			return fmt.Errorf("usage: arbiter init [--no-executor] [--remove] [--embedded-engine] — see: arbiter init --help")
		}
		msg, err := deploy.InitWithOptions(root, opts)
		if err != nil {
			return err
		}
		fmt.Print(msg)
		return nil
	case "adopt":
		if len(os.Args) != 2 {
			return fmt.Errorf("usage: arbiter adopt")
		}
		report, err := deploy.Adopt(root)
		if err != nil {
			return err
		}
		fmt.Print(report.String())
		return nil
	case "status":
		if len(os.Args) > 3 || (len(os.Args) == 3 && os.Args[2] != "--json") {
			return fmt.Errorf("usage: arbiter status [--json]")
		}
		status, err := cli.Status(root)
		if err != nil {
			return err
		}
		if len(os.Args) == 3 {
			data, err := cli.JSON(status)
			if err != nil {
				return err
			}
			fmt.Print(string(data))
			return nil
		}
		fmt.Print(cli.FormatStatus(status))
		return nil
	case "report":
		jsonOut, matchID, err := cli.ParseReportArgs(os.Args[2:])
		if err != nil {
			return fmt.Errorf("usage: arbiter report [--json] [match_id]")
		}
		report, err := cli.Report(root, matchID)
		if err != nil {
			return err
		}
		if jsonOut {
			data, err := cli.JSON(report)
			if err != nil {
				return err
			}
			fmt.Print(string(data))
			return nil
		}
		fmt.Print(cli.FormatReport(report))
		return nil
	case "serve":
		if len(os.Args) != 3 {
			return fmt.Errorf("usage: arbiter serve <seat>")
		}
		return seat.Run(context.Background(), root, os.Args[2])
	case "hook":
		if len(os.Args) != 3 || os.Args[2] != "stop" {
			return fmt.Errorf("usage: arbiter hook stop")
		}
		_, _ = io.Copy(io.Discard, os.Stdin) // 宿主写入事件 JSON;门控只依赖对局状态
		decision, err := match.New(root, "hook").StopGate()
		if err != nil {
			return err // 非零退出但无 block 决策:门控故障时放行停止(fail-open),错误进 stderr
		}
		if !decision.Allow {
			data, err := json.Marshal(map[string]any{"decision": "block", "reason": decision.Reason})
			if err != nil {
				return err
			}
			fmt.Println(string(data))
		}
		return nil
	default:
		return fmt.Errorf("usage: arbiter init [flags] | adopt | status [--json] | report [--json] [match_id] | serve <seat> | hook stop | cc -- <real-compiler> [args...]")
	}
}

const usage = `arbiter — referee-adjudicated dev loop for C codebases

Usage:
  arbiter <command>

Commands:
  init     Wire the current repository. One command, idempotent, seconds, never
           builds or indexes. Delivers the starter openings and the bundled
           gdb-mcp + perf-mcp diagnostic servers; the engine ships inside this
           binary — the only system prerequisite is python3 (>= 3.9). Re-run
           any time, including after upgrading arbiter. See: arbiter init --help
  adopt    Migrate a legacy chess/crun-mcp/cipher-2 deployment into .arbiter/.
  status   Deployment, engine, match, and runs status (add --json for machines).
  report   Journal + run evidence for a finished match (--json supported).
  serve    Run a seat MCP server (player|curator|executor). Spawned by Claude
           Code from .mcp.json and the agent files — not run by hand.
  hook     The Stop-hook gate (arbiter hook stop). Wired by init — not run by hand.
  cc       The per-TU compiler shim (arbiter cc -- <real-cc> ...). Installed
           into recipes by /arbiter-intro — not run by hand.
  help     Show this help. init also accepts -h / --help.

Inside Claude Code (after init):
  /arbiter-intro            once per repo: adjudicated bootstrap (recipes, shim, first index)
  /arbiter-play <request>   every request: play a refereed match
  /playbook-create          capture knowledge as a new playbook

Docs: README.md (quick start) · docs/user-guide.md (the manual)
`

const initHelp = `arbiter init — wire the current repository (idempotent, seconds, no build)

Writes or merges, always preserving foreign content:
  .mcp.json                arbiter (serve player) + gdb-mcp + perf-mcp entries
  .claude/agents/          curator, executor, implementer, test-author, debugger
                           (seat credential injected, 0600, gitignored)
  .claude/skills/          arbiter-play, arbiter-intro, playbook-create
  .claude/settings.json    deny rules + the Stop-hook gate
  .arbiter/playbook/       FORMAT.md + starter openings, write-if-missing:
                           fix-reported-bug, hunt-latent-bugs, build-feature,
                           fix-slow-path, freeplay, gold-digger,
                           recipe-derivation, regression-triage
  .arbiter/                config.yml, recipes.yaml scaffolds; seat key;
                           run/engines.json (verified engine record)
  .gitignore               derived-state entries

Engine resolution — automatic, in this order (ADR-0011):
  1. an installed arbiter-engine package for python3          (preferred)
  2. the engine embedded in this binary -> .arbiter/engine/   (zero extra installs)
  3. python3 missing/broken -> typed error; installing python3 (>= 3.9)
     is the only setup you will ever be asked to do.

Flags:
  --no-executor      skip the executor-seat agents (incl. arbiter-debugger)
  --embedded-engine  force the embedded engine even when a package is installed
  --remove           reverse everything init wrote and nothing else

Exit 0 on success.
`

func isHelp(arg string) bool {
	return arg == "help" || arg == "-h" || arg == "--help"
}

func wantsHelp(rest []string) bool {
	for _, arg := range rest {
		if isHelp(arg) {
			return true
		}
	}
	return false
}
