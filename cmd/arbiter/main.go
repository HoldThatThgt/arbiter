package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

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
		return fmt.Errorf("usage: arbiter init [flags] | serve <seat> | hook stop | cc -- <real-compiler> [args...]")
	}
	root, err := os.Getwd()
	if err != nil {
		return err
	}
	switch os.Args[1] {
	case "init":
		fs := flag.NewFlagSet("init", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		opts := deploy.Options{}
		fs.BoolVar(&opts.NoExecutor, "no-executor", false, "skip executor agent")
		fs.BoolVar(&opts.Remove, "remove", false, "remove generated init wiring")
		fs.BoolVar(&opts.EmbeddedEngine, "embedded-engine", false, "deny edits to embedded engine files")
		openings := fs.Bool("openings", false, "install opening playbooks")
		if err := fs.Parse(os.Args[2:]); err != nil || fs.NArg() != 0 {
			return fmt.Errorf("usage: arbiter init [--openings] [--no-executor] [--remove] [--embedded-engine]")
		}
		_ = openings
		msg, err := deploy.InitWithOptions(root, opts)
		if err != nil {
			return err
		}
		fmt.Print(msg)
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
		return fmt.Errorf("usage: arbiter init [flags] | serve <seat> | hook stop | cc -- <real-compiler> [args...]")
	}
}
