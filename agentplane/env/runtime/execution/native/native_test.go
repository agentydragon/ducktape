package native

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestProcessResumeAndOutput(t *testing.T) {
	server := New()
	defer server.Close()
	session, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	server.Detach(session.ID())
	resumed, err := server.OpenSession(session.ID())
	if err != nil || resumed != session {
		t.Fatalf("resume: %v", err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "p", Argv: []string{"/bin/sh", "-c", "printf out; printf err >&2"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	var after *uint64
	var got strings.Builder
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		result, err := resumed.Read(context.Background(), "p", after, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range result.Chunks {
			got.Write(c.Data)
		}
		if result.NextSeq > 0 {
			n := result.NextSeq - 1
			after = &n
		}
		if result.Closed {
			break
		}
	}
	if !strings.Contains(got.String(), "out") || !strings.Contains(got.String(), "err") {
		t.Fatalf("output %q", got.String())
	}
}

func TestFileRange(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	path := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(path, []byte("hello"), 0600); err != nil {
		t.Fatal(err)
	}
	uri := (&url.URL{Scheme: "file", Path: path}).String()
	if err := session.OpenFile("h", uri); err != nil {
		t.Fatal(err)
	}
	b, eof, err := session.ReadFile("h", 2, 10)
	if err != nil || !eof || string(b) != "llo" {
		t.Fatalf("%q %v %v", b, eof, err)
	}
	if err := session.CloseFile("h"); err != nil {
		t.Fatal(err)
	}
}

func TestEnvironmentPolicyAndRestrictedVariables(t *testing.T) {
	t.Setenv("NATIVE_TEST_SECRET", "hide")
	t.Setenv("NATIVE_TEST_VISIBLE", "show")
	env, err := BuildEnvironment(&EnvPolicy{Inherit: "all", Set: map[string]string{"NATIVE_TEST_ADDED": "yes"}, IncludeOnly: []string{"NATIVE_TEST_*"}}, map[string]string{"NODE_REPL_AUTH_TOKEN": "not allowed"})
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(env, "\n")
	if strings.Contains(joined, "NATIVE_TEST_SECRET") || strings.Contains(joined, "NODE_REPL_AUTH_TOKEN") || !strings.Contains(joined, "NATIVE_TEST_VISIBLE=show") || !strings.Contains(joined, "NATIVE_TEST_ADDED=yes") {
		t.Fatal(joined)
	}
}

func TestPTY(t *testing.T) {
	server := New()
	defer server.Close()
	session, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "pty", Argv: []string{"/bin/sh", "-c", "test -t 0 && printf PTY_OK"}, Cwd: cwd, TTY: true}); err != nil {
		t.Fatal(err)
	}
	var after *uint64
	var got strings.Builder
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		result, err := session.Read(context.Background(), "pty", after, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range result.Chunks {
			if c.Stream != "pty" {
				t.Fatalf("stream %q", c.Stream)
			}
			got.Write(c.Data)
		}
		if result.NextSeq > 0 {
			n := result.NextSeq - 1
			after = &n
		}
		if result.Closed {
			break
		}
	}
	if !strings.Contains(got.String(), "PTY_OK") {
		t.Fatalf("output %q", got.String())
	}
}

func TestSessionAttachmentsProtectResources(t *testing.T) {
	server := New()
	defer server.Close()
	session, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	_, err = server.OpenSession(session.ID())
	if !errors.Is(err, ErrSessionAttached) {
		t.Fatalf("simultaneous resume: %v", err)
	}
	if err := server.RemoveSession(session.ID()); err == nil {
		t.Fatal("removed attached session")
	}
	server.Detach(session.ID())
	if err := server.RemoveSession(session.ID()); err != nil {
		t.Fatal(err)
	}
	if _, err := server.OpenSession(session.ID()); !errors.Is(err, ErrUnknownSession) {
		t.Fatalf("resume removed session: %v", err)
	}
}

func TestOutputAndTerminalEventsHaveDistinctSequences(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "events", Argv: []string{"/bin/sh", "-c", "printf test"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		result, err := session.Read(context.Background(), "events", nil, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if result.Closed {
			if result.ExitSeq == nil || result.ClosedSeq == nil || *result.ExitSeq == *result.ClosedSeq {
				t.Fatalf("invalid terminal sequences: %+v", result)
			}
			for _, c := range result.Chunks {
				if c.Seq == *result.ExitSeq || c.Seq == *result.ClosedSeq {
					t.Fatalf("overlapping sequence: %+v", result)
				}
			}
			return
		}
	}
	t.Fatal("process never closed")
}

func TestStaleExpiryCannotDeleteRenewedSession(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	server.Detach(session.ID())
	stale := session.expiryGeneration
	if _, err := server.OpenSession(session.ID()); err != nil {
		t.Fatal(err)
	}
	server.Detach(session.ID())
	server.expire(session.ID(), session, stale)
	if _, err := server.Session(session.ID()); err != nil {
		t.Fatalf("stale expiration removed session: %v", err)
	}
}

func TestReadCancellationDoesNotKillProcess(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "wait", Argv: []string{"/bin/sh", "-c", "sleep 1; printf alive"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := session.Read(ctx, "wait", nil, 4096, time.Minute); !errors.Is(err, context.Canceled) {
		t.Fatalf("read cancellation: %v", err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		r, err := session.Read(context.Background(), "wait", nil, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		for _, chunk := range r.Chunks {
			if strings.Contains(string(chunk.Data), "alive") {
				return
			}
		}
	}
	t.Fatal("process did not survive canceled read")
}

func TestReclaimOnlyClosedMatchingProcess(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "p", Argv: []string{"/bin/sh", "-c", "printf output"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	p, err := session.get("p")
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		r, err := session.Read(context.Background(), "p", nil, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if r.Closed {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("not closed")
		}
	}
	session.reclaim("p", p)
	if _, err := session.get("p"); err == nil {
		t.Fatal("closed process retained")
	}
	if n := server.processCount.Load(); n != 0 {
		t.Fatalf("process count = %d", n)
	}
	if n := server.outputBytes.Load(); n != 0 {
		t.Fatalf("output bytes = %d", n)
	}
	if err := session.Start(StartRequest{ProcessID: "p", Argv: []string{"/bin/sh", "-c", "sleep 1"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	session.reclaim("p", p)
	if _, err := session.get("p"); err != nil {
		t.Fatal("stale reclaim removed successor")
	}
}

func TestWriteIDAccountingAndCleanup(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "cat", Argv: []string{"/bin/cat"}, Cwd: cwd, PipeStdin: true}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		status, err := session.WriteContext(context.Background(), "cat", "same", []byte("hello"))
		if err != nil || status != "accepted" {
			t.Fatalf("write: %s %v", status, err)
		}
	}
	if n := server.writeCount.Load(); n != 1 {
		t.Fatalf("writes = %d", n)
	}
	session.close()
	if n := server.writeCount.Load(); n != 0 {
		t.Fatalf("retained writes after cleanup = %d", n)
	}
}

func TestReadReturnsExitWhileDescendantHoldsOutput(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "p", Argv: []string{"/bin/sh", "-c", "sleep 2 & exit 0"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	r, err := session.Read(ctx, "p", nil, 4096, 5*time.Second)
	if err != nil || !r.Exited || r.ExitSeq == nil {
		t.Fatalf("missing exit: %+v %v", r, err)
	}
}

func TestSignalExitCode(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "p", Argv: []string{"/bin/sleep", "10"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	if running, err := session.Terminate("p"); err != nil || !running {
		t.Fatalf("terminate %t %v", running, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r, err := session.Read(ctx, "p", nil, 4096, 3*time.Second)
	if err != nil || r.ExitCode == nil || *r.ExitCode != 137 {
		t.Fatalf("exit: %+v %v", r, err)
	}
}

func TestCloseConcurrentWithExpiration(t *testing.T) {
	for i := 0; i < 100; i++ {
		server := New()
		session, _ := server.OpenSession("")
		server.Detach(session.ID())
		generation := session.expiryGeneration
		done := make(chan struct{})
		go func() { defer close(done); server.expire(session.ID(), session, generation) }()
		if err := server.Close(); err != nil {
			t.Fatal(err)
		}
		<-done
	}
}

func TestFullGlobalBufferReusesOwnRetainedBytes(t *testing.T) {
	server := New()
	globalOutput := int64(server.limits.MaxOutputBytes)
	server.outputBytes.Store(globalOutput)
	p := &process{server: server, changed: make(chan struct{}), chunks: []Chunk{{Seq: 1, Stream: "stdout", Data: []byte("old")}}, bytes: 3, next: 2}
	var wg sync.WaitGroup
	wg.Add(1)
	p.readPipe("stdout", strings.NewReader("new"), &wg)
	if p.failure != nil || len(p.chunks) != 1 || string(p.chunks[0].Data) != "new" {
		t.Fatalf("failure=%v chunks=%v", p.failure, p.chunks)
	}
	if n := server.outputBytes.Load(); n != globalOutput {
		t.Fatalf("output accounting: %d", n)
	}
}

func TestExitedLeaderAnchorsGroupUntilSessionCleanup(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "anchor", Argv: []string{"/bin/sh", "-c", "/bin/sleep 30 & echo $!; exit 0"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	p, err := session.get("anchor")
	if err != nil {
		t.Fatal(err)
	}
	var output strings.Builder
	var after *uint64
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		r, err := session.Read(ctx, "anchor", after, 4096, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range r.Chunks {
			output.Write(c.Data)
		}
		if r.NextSeq > 0 {
			n := r.NextSeq - 1
			after = &n
		}
		if r.Exited && strings.TrimSpace(output.String()) != "" {
			break
		}
	}
	childPID, err := strconv.Atoi(strings.TrimSpace(output.String()))
	if err != nil {
		t.Fatalf("child pid %q: %v", output.String(), err)
	}
	leaderStat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", p.cmd.Process.Pid))
	if err != nil || !strings.Contains(string(leaderStat), ") Z ") {
		t.Fatalf("leader must remain zombie: %s %v", leaderStat, err)
	}
	session.close()
	if _, err := os.Stat(fmt.Sprintf("/proc/%d", p.cmd.Process.Pid)); !os.IsNotExist(err) {
		t.Fatalf("leader not reaped: %v", err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", childPID))
		if os.IsNotExist(err) || strings.Contains(string(stat), ") Z ") {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("child %d survived cleanup", childPID)
}

func TestSessionChangesWakeForProcessStartAndExit(t *testing.T) {
	server := New()
	defer server.Close()
	session, _ := server.OpenSession("")
	changed := session.Changes()
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "wake", Argv: []string{"/bin/sleep", "0.2"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-changed:
	case <-time.After(time.Second):
		t.Fatal("no process start wake")
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		changed = session.Changes()
		r, err := session.Read(context.Background(), "wake", nil, 4096, 0)
		if err != nil {
			t.Fatal(err)
		}
		if r.Exited {
			return
		}
		select {
		case <-changed:
		case <-time.After(time.Second):
			t.Fatal("no process exit wake")
		}
	}
	t.Fatal("process did not exit")
}

func TestLostChildWaitOwnershipIsFatal(t *testing.T) {
	server := New()
	server.waitChild = func(int, *childSiginfo) error { return syscall.ECHILD }
	defer server.Close()
	session, _ := server.OpenSession("")
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "lost", Argv: []string{"/bin/sleep", "30"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	p, _ := session.get("lost")
	select {
	case err := <-server.Fatal():
		if !errors.Is(err, syscall.ECHILD) {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("missing fatal ownership failure")
	}
	p.mu.Lock()
	lost := p.ownershipLost
	p.mu.Unlock()
	if !lost {
		t.Fatal("ownership not marked lost")
	}
	// The injected failure did not actually transfer ownership; clean up the test child.
	if err := p.cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = p.cmd.Wait()
}
