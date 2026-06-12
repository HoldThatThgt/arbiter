package seat

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// TestMain 的 re-exec 模式:子进程模式下本测试二进制就是一个真实席位服务器。
func TestMain(m *testing.M) {
	if role := os.Getenv("ARBITER_SEAT_STUB"); role != "" {
		if err := Run(context.Background(), os.Getenv("ARBITER_SEAT_STUB_ROOT"), role); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// TestMatchVisibleAcrossSeatProcessesDespiteHostileCwd 是用户实测回归:
// "curator 子代理装载的对局,主会话 player 永远看不见(no active match)"。
// 状态共享本就是文件级的(.arbiter/match/run/state.json + flock);断裂点
// 在各席位进程以 cwd 推导仓根,而宿主拉起主会话服务器与子代理服务器的
// cwd 互不保证。本测试用两个真实席位进程、刻意互异且都不等于仓根的 cwd、
// 同一显式 root:curator 装载对局后,player 必须立即看见活跃步骤。
func TestMatchVisibleAcrossSeatProcessesDespiteHostileCwd(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "end.md", endBook)
	key := "0123456789abcdef0123456789abcdef"
	matchDir := filepath.Join(root, ".arbiter", "match")
	if err := os.MkdirAll(matchDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(matchDir, "seat.key"), []byte(key+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	// curator 进程:cwd = "/"(对仓根毫无线索)。
	curator := spawnSeatProcess(t, root, Curator, "/", key)
	defer curator.stop()
	curator.handshake(t)
	load := curator.call(t, 2, "LoadPlayBook", map[string]any{"name": "endgame"})
	if strings.Contains(load, `"isError":true`) {
		t.Fatalf("LoadPlayBook failed: %s", load)
	}
	curator.stop()

	// player 进程:cwd = 另一个无关目录。对局必须可见。
	player := spawnSeatProcess(t, root, Player, t.TempDir(), key)
	defer player.stop()
	player.handshake(t)
	show := player.call(t, 2, "ShowStepJob", map[string]any{})
	if strings.Contains(show, playbook.CodeNoActiveMatch) {
		t.Fatalf("player cannot see the curator-loaded match: %s", show)
	}
	if !strings.Contains(show, `"only"`) {
		t.Fatalf("ShowStepJob missing the loaded step: %s", show)
	}
}

type seatProc struct {
	cmd    *exec.Cmd
	stdin  *json.Encoder
	stdout *bufio.Reader
	closer func()
}

func spawnSeatProcess(t *testing.T, root, role, cwd, key string) *seatProc {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(self)
	cmd.Dir = cwd
	cmd.Env = append(os.Environ(),
		"ARBITER_SEAT_STUB="+role,
		"ARBITER_SEAT_STUB_ROOT="+root,
		playbook.SeatEnvKey+"="+key,
	)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	proc := &seatProc{
		cmd:    cmd,
		stdin:  json.NewEncoder(stdin),
		stdout: bufio.NewReader(stdout),
	}
	var once bool
	proc.closer = func() {
		if once {
			return
		}
		once = true
		_ = stdin.Close() // 席位约定:stdin EOF 即自退
		done := make(chan struct{})
		go func() { _ = cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			_ = cmd.Process.Kill()
			<-done
		}
	}
	return proc
}

func (p *seatProc) stop() { p.closer() }

func (p *seatProc) send(t *testing.T, payload map[string]any) {
	t.Helper()
	if err := p.stdin.Encode(payload); err != nil {
		t.Fatal(err)
	}
}

func (p *seatProc) read(t *testing.T) string {
	t.Helper()
	line, err := p.stdout.ReadString('\n')
	if err != nil {
		t.Fatalf("seat stdout closed: %v (last=%q)", err, line)
	}
	return line
}

func (p *seatProc) handshake(t *testing.T) {
	t.Helper()
	p.send(t, map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "initialize",
		"params": map[string]any{
			"protocolVersion": "2025-06-18",
			"capabilities":    map[string]any{},
			"clientInfo":      map[string]any{"name": "crossproc-test", "version": "v1"},
		},
	})
	if line := p.read(t); !strings.Contains(line, `"serverInfo"`) {
		t.Fatalf("unexpected initialize response: %s", line)
	}
	p.send(t, map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})
}

func (p *seatProc) call(t *testing.T, id int, tool string, arguments map[string]any) string {
	t.Helper()
	p.send(t, map[string]any{
		"jsonrpc": "2.0", "id": id, "method": "tools/call",
		"params": map[string]any{"name": tool, "arguments": arguments},
	})
	return p.read(t)
}
