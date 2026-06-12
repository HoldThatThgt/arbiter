package match

import (
	"context"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
)

// execEngine is the subset of *engineclient.Engine the async run-goal
// lifecycle needs; a seam so tests can count spawns with a fake instead of a
// real Python child.
type execEngine interface {
	StartRun(ctx context.Context, spec, meta any) (engineclient.RunStart, error)
	RunStatus(ctx context.Context, runID string) (engineclient.RunStatus, error)
	Poisoned() bool
	Respawn(ctx context.Context) error
	Close() error
}

// goalExecEngine returns the Store's cached exec engine, lazily spawning it
// on first need and respawning a poisoned child before reuse (the seat's
// respawnIfPoisoned pattern). The engine outlives a single CheckStepJob call
// so consecutive GoalPending polls do not each pay interpreter startup.
// Callers invoke it outside the match file lock — same discipline as the
// per-call Spawn it replaces.
func (s *Store) goalExecEngine(ctx context.Context) (execEngine, error) {
	s.engineMu.Lock()
	defer s.engineMu.Unlock()
	if s.goalEngine != nil {
		if s.goalEngine.Poisoned() {
			if err := s.goalEngine.Respawn(ctx); err != nil {
				_ = s.goalEngine.Close()
				s.goalEngine = nil
				return nil, err
			}
		}
		return s.goalEngine, nil
	}
	spawn := s.spawnExec
	if spawn == nil {
		spawn = spawnExecEngine
	}
	engine, err := spawn(ctx, s.Root)
	if err != nil {
		return nil, err
	}
	s.goalEngine = engine
	return engine, nil
}

func spawnExecEngine(ctx context.Context, root string) (execEngine, error) {
	engine, err := engineclient.Spawn(ctx, engineclient.RoleExec, root)
	if err != nil {
		return nil, err
	}
	return engine, nil
}

// closeGoalEngine closes and clears the cached exec engine. It runs on every
// path that settles or discards a GoalPending: the goal lifecycle is over, so
// the child must not outlive it. Retryable engine errors keep the cache (a
// poisoned child is respawned on the next poll instead).
func (s *Store) closeGoalEngine() {
	s.engineMu.Lock()
	engine := s.goalEngine
	s.goalEngine = nil
	s.engineMu.Unlock()
	if engine != nil {
		_ = engine.Close()
	}
}

// CloseEngines closes the engine children cached by the Store. Hosts that
// keep one Store for the process lifetime (the seat runtime) should call it
// at shutdown; short-lived Stores may skip it — the child exits on stdin EOF
// when its parent does.
func (s *Store) CloseEngines() {
	s.closeGoalEngine()
}
