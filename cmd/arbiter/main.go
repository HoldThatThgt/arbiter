package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/seat"
)

const usage = `arbiter — referee-adjudicated dev loop for C codebases

Usage:
  arbiter <command>

Commands:
  init    Wire the current repository. One command, idempotent, finishes in
          seconds, never builds or indexes. Includes the bundled gdb-mcp and
          perf-mcp diagnostic servers — the only system prerequisite is
          python3 (>= 3.9). Re-run any time, including after upgrading arbiter.
  serve   Run a seat MCP server (player|curator|executor). Spawned by Claude
          Code from .mcp.json and the agent files — not run by hand.
  hook    The Stop-hook gate (arbiter hook stop). Wired by init — not run by hand.
  help    Show this help. Every command also accepts -h / --help.

Inside Claude Code (after init):
  /arbiter-play <request>   play a refereed match
  /playbook-create          draft and register a playbook
  (/arbiter-intro — the adjudicated bootstrap match — lands with milestone M7)

Docs: README.md (quick start) · docs/user-guide.md (the manual)
`

const initHelp = `arbiter init — wire the current repository (idempotent, seconds, no build)

Writes or merges, always preserving foreign content:
  .mcp.json                arbiter (serve player) + gdb-mcp + perf-mcp entries
  .claude/agents/          arbiter-curator.md and arbiter-debugger.md
                           (seat credential injected, 0600, gitignored)
  .claude/skills/          arbiter-play, playbook-create
  .claude/settings.json    deny rules + the Stop-hook gate
  .arbiter/match/          seat key, FORMAT.md, and four starter openings —
                           fix-reported-bug, hunt-latent-bugs, build-feature,
                           fix-slow-path (write-if-missing: your edits survive)
  .gitignore               derived-state entries

Engine resolution — automatic, in this order:
  1. an installed arbiter-engine package for python3         (preferred)
  2. the engine embedded in this binary -> .arbiter/engine/  (zero extra installs)
  3. no python3 on PATH -> diagnostics are skipped; install python3 (>= 3.9)
     and re-run arbiter init — nothing else to install.

One follow-up is printed on success: the executor agent template to paste into
.claude/agents/arbiter-executor.md (automated in milestone M7).

No flags. Exit 0 on success.
`

const serveHelp = `arbiter serve <player|curator|executor> — run a seat MCP server on stdio

Seats are how models talk to the referee; a tool not registered for a seat
does not exist for that seat. Claude Code spawns these from .mcp.json (player)
and the agent files (curator/executor — these require ARBITER_SEAT_KEY).
You never run this by hand.
`

const hookHelp = `arbiter hook stop — the Stop-hook gate

Wired into .claude/settings.json by arbiter init. While a match is live the
gate blocks the model from stopping on its own; user interrupts are never
blocked. Fails open: a gate fault allows the stop and reports on stderr.
You never run this by hand.
`

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	args := os.Args[1:]
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("missing command")
	}
	if isHelp(args[0]) {
		fmt.Print(usage)
		return nil
	}
	root, err := os.Getwd()
	if err != nil {
		return err
	}
	switch args[0] {
	case "init":
		if wantsHelp(args[1:]) {
			fmt.Print(initHelp)
			return nil
		}
		if len(args) != 1 {
			return fmt.Errorf("arbiter init takes no arguments — see: arbiter init --help")
		}
		msg, err := deploy.Init(root)
		if err != nil {
			return err
		}
		fmt.Print(msg)
		return nil
	case "serve":
		if wantsHelp(args[1:]) {
			fmt.Print(serveHelp)
			return nil
		}
		if len(args) != 2 {
			return fmt.Errorf("usage: arbiter serve <player|curator|executor> — see: arbiter serve --help")
		}
		return seat.Run(context.Background(), root, args[1])
	case "hook":
		if wantsHelp(args[1:]) {
			fmt.Print(hookHelp)
			return nil
		}
		if len(args) != 2 || args[1] != "stop" {
			return fmt.Errorf("usage: arbiter hook stop — see: arbiter hook --help")
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
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("unknown command: %s", args[0])
	}
}

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
