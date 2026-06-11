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

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: arbiter init | serve <seat>")
	}
	root, err := os.Getwd()
	if err != nil {
		return err
	}
	switch os.Args[1] {
	case "init":
		if len(os.Args) != 2 {
			return fmt.Errorf("usage: arbiter init")
		}
		msg, err := deploy.Init(root)
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
		return fmt.Errorf("usage: arbiter init | serve <seat> | hook stop")
	}
}
