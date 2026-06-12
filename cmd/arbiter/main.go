package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/HoldThatThgt/arbiter/internal/cli"
	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/guard"
	"github.com/HoldThatThgt/arbiter/internal/interpose"
	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/seat"
)

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "cc" {
		cwd, err := os.Getwd()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		args := os.Args[2:]
		root := ""
		if len(args) >= 2 && args[0] == "--root" {
			root = args[1]
			args = args[2:]
			if !filepath.IsAbs(root) {
				root = filepath.Join(cwd, root)
			}
			root = filepath.Clean(root)
		} else {
			// ADR-0014: cwd is not load-bearing — builds compile from subdirs
			// and out-of-tree build dirs, so the journal root is discovered,
			// not assumed.
			root = interpose.DiscoverRoot(cwd)
		}
		os.Exit(interpose.Run(root, cwd, args, os.Stdin, os.Stdout, os.Stderr))
	}
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: arbiter init [flags] | adopt | status [--json] | report [--json] [match_id] | serve <seat> | hook stop | cc [--root DIR] -- <real-compiler> [args...]")
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
		seatName, seatRoot, err := parseRootArgs(os.Args[2:], root, 1)
		if err != nil {
			return fmt.Errorf("usage: arbiter serve <player|curator|executor> [--root DIR]")
		}
		return seat.Run(context.Background(), seatRoot, seatName)
	case "hook":
		sub, hookRoot, err := parseRootArgs(os.Args[2:], root, 1)
		if err != nil || (sub != "stop" && sub != "guard" && sub != "subagent-stop") {
			return fmt.Errorf("usage: arbiter hook <stop|guard|subagent-stop> [--root DIR]")
		}
		root = hookRoot
		if sub == "subagent-stop" {
			payload, err := io.ReadAll(os.Stdin)
			if err != nil {
				return nil // fail-open:门控故障不阻塞子代理
			}
			var input struct {
				TranscriptPath string `json:"transcript_path"`
				AgentID        string `json:"agent_id"`
			}
			if err := json.Unmarshal(payload, &input); err != nil {
				return nil
			}
			transcript := match.ResolveSubagentTranscript(input.TranscriptPath, input.AgentID)
			ids := match.ExtractDispatchTaskIDs(transcript)
			if len(ids) == 0 {
				return nil
			}
			decision, err := match.New(root, "hook").SubagentStopGate(ids)
			if err != nil {
				return err // 非零退出但无 block 决策:门控故障放行(fail-open),错误进 stderr
			}
			if !decision.Allow {
				data, err := json.Marshal(map[string]any{"decision": "block", "reason": decision.Reason})
				if err != nil {
					return err
				}
				fmt.Println(string(data))
			}
			return nil
		}
		if sub == "guard" {
			payload, err := io.ReadAll(os.Stdin)
			if err != nil {
				return nil // fail-open:门控故障不阻塞会话
			}
			decision := guard.Decide(root, match.FrozenTestPaths(root), payload)
			if decision.Deny {
				out, err := json.Marshal(map[string]any{
					"hookSpecificOutput": map[string]any{
						"hookEventName":            "PreToolUse",
						"permissionDecision":       "deny",
						"permissionDecisionReason": decision.Reason,
					},
				})
				if err != nil {
					return nil
				}
				fmt.Println(string(out))
			}
			return nil
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
		return fmt.Errorf("usage: arbiter init [flags] | adopt | status [--json] | report [--json] [match_id] | serve <seat> | hook stop | cc [--root DIR] -- <real-compiler> [args...]")
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
  cc       The per-TU compiler shim (arbiter cc [--root DIR] -- <real-cc> ...). Installed
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
  .arbiter/playbook/       FORMAT.md + starter openings, refreshed to the shipped
                           version every init (customize by forking to a new name):
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

// parseRootArgs 解析 "<positional…> [--root DIR]" 形态:返回首个位置参数与
// 解析到的绝对 root(缺省 = 调用方传入的 cwd)。席位与 Stop 门控的仓根
// 必须显式可指定 —— 宿主拉起子进程的 cwd 不可假设(参见 -32000 教训),
// init 写出的所有条目都携带 --root。
func parseRootArgs(args []string, fallback string, positional int) (string, string, error) {
	var positionals []string
	rootDir := fallback
	for i := 0; i < len(args); i++ {
		if args[i] == "--root" {
			if i+1 >= len(args) {
				return "", "", fmt.Errorf("--root requires a directory")
			}
			rootDir = args[i+1]
			i++
			continue
		}
		positionals = append(positionals, args[i])
	}
	if len(positionals) != positional {
		return "", "", fmt.Errorf("expected %d positional argument(s)", positional)
	}
	abs, err := filepath.Abs(rootDir)
	if err != nil {
		return "", "", err
	}
	return positionals[0], abs, nil
}
