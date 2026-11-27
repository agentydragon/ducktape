package native

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os/exec"
	"sync"
	"syscall"
	"testing"
	"time"
)

type testManaged struct {
	exit    chan int
	signals chan syscall.Signal
}

func (p *testManaged) Wait(ctx context.Context) (int, error) {
	select {
	case n := <-p.exit:
		return n, nil
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (p *testManaged) Signal(_ context.Context, signal syscall.Signal) error {
	p.signals <- signal
	return nil
}
func (p *testManaged) Release(context.Context) error { return nil }

// blockingRelease reports each Release call and holds it until unblock is closed.
type blockingRelease struct {
	testManaged
	released chan struct{}
	unblock  chan struct{}
}

func (p *blockingRelease) Release(context.Context) error {
	p.released <- struct{}{}
	<-p.unblock
	return nil
}

// requireWithin fails the test unless op finishes while a slow command owner call is held open.
func requireWithin(t *testing.T, what string, op func() error) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- op() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("%s: %v", what, err)
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("%s blocked behind a slow command owner call", what)
	}
}

func TestSlowLauncherDoesNotBlockSessionAndCloseReleasesIt(t *testing.T) {
	entered := make(chan struct{})
	unblock := make(chan struct{})
	var unblockOnce sync.Once
	t.Cleanup(func() { unblockOnce.Do(func() { close(unblock) }) })
	slow := &blockingRelease{testManaged: testManaged{exit: make(chan int, 1)}, released: make(chan struct{}, 1), unblock: make(chan struct{})}
	close(slow.unblock)
	launcher := func(_ context.Context, cmd *exec.Cmd, _ bool) (ManagedCommand, error) {
		if cmd.Args[1] == "slow" {
			close(entered)
			<-unblock
			return slow, nil
		}
		return &testManaged{exit: make(chan int, 1)}, nil
	}
	server, err := NewWithLauncher(DefaultLimits(), launcher)
	if err != nil {
		t.Fatal(err)
	}
	sess, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := sess.Start(StartRequest{ProcessID: "fast", Argv: []string{"/bin/true", "fast"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	slowStarted := make(chan error, 1)
	go func() {
		slowStarted <- sess.Start(StartRequest{ProcessID: "slow", Argv: []string{"/bin/true", "slow"}, Cwd: cwd})
	}()
	<-entered

	requireWithin(t, "read of another process", func() error {
		_, err := sess.Read(context.Background(), "fast", nil, 1024, 0)
		return err
	})
	if err := sess.Start(StartRequest{ProcessID: "slow", Argv: []string{"/bin/true", "slow"}, Cwd: cwd}); !errors.Is(err, ErrProcessExists) {
		t.Fatalf("duplicate start during launch = %v, want ErrProcessExists", err)
	}

	closed := make(chan error, 1)
	go func() { closed <- server.Close() }()
	// Reads report unknown processes once the session is closed.
	for {
		if _, err := sess.Read(context.Background(), "fast", nil, 1024, 0); errors.Is(err, ErrUnknownProcess) {
			break
		}
	}
	unblockOnce.Do(func() { close(unblock) })
	if err := <-slowStarted; err != nil {
		t.Fatalf("slow start: %v", err)
	}
	if err := <-closed; err != nil {
		t.Fatal(err)
	}
	select {
	case <-slow.released:
	default:
		t.Fatal("close did not release the process published during its launch")
	}
}

func TestReclaimReleaseDoesNotBlockSession(t *testing.T) {
	retained := &blockingRelease{testManaged: testManaged{exit: make(chan int, 1)}, released: make(chan struct{}, 1), unblock: make(chan struct{})}
	launcher := func(_ context.Context, cmd *exec.Cmd, _ bool) (ManagedCommand, error) {
		if cmd.Args[1] == "retained" {
			return retained, nil
		}
		return &testManaged{exit: make(chan int, 1)}, nil
	}
	server, err := NewWithLauncher(DefaultLimits(), launcher)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	// Runs before server.Close, which waits for the held Release on a failure.
	defer close(retained.unblock)
	sess, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	for _, id := range []string{"retained", "other"} {
		if err := sess.Start(StartRequest{ProcessID: id, Argv: []string{"/bin/true", id}, Cwd: cwd}); err != nil {
			t.Fatal(err)
		}
	}
	p, err := sess.get("retained")
	if err != nil {
		t.Fatal(err)
	}
	retained.exit <- 0
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		r, err := sess.Read(ctx, "retained", nil, 1024, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if r.Closed {
			break
		}
	}
	go sess.reclaim("retained", p)
	<-retained.released

	requireWithin(t, "read of another process", func() error {
		_, err := sess.Read(context.Background(), "other", nil, 1024, 0)
		return err
	})
	requireWithin(t, "process listing", func() error {
		if ids := sess.ProcessIDs(); len(ids) != 1 || ids[0] != "other" {
			return fmt.Errorf("process IDs = %v, want [other]", ids)
		}
		return nil
	})
}

func TestManagedCommandsKeepSessionAfterWorkloadOOM(t *testing.T) {
	processes := make(chan *testManaged, 2)
	launcher := func(_ context.Context, cmd *exec.Cmd, _ bool) (ManagedCommand, error) {
		p := &testManaged{exit: make(chan int, 1), signals: make(chan syscall.Signal, 1)}
		if _, err := io.WriteString(cmd.Stdout, "hello"); err != nil {
			return nil, err
		}
		processes <- p
		return p, nil
	}
	server, err := NewWithLauncher(DefaultLimits(), launcher)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	sess, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	start := func(id string) {
		t.Helper()
		if err := sess.Start(StartRequest{ProcessID: id, Argv: []string{"/bin/true"}, Cwd: cwd}); err != nil {
			t.Fatal(err)
		}
	}
	start("oom")
	p := <-processes
	p.exit <- 137
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := sess.Read(ctx, "oom", nil, 1024, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	for !out.Exited {
		out, err = sess.Read(ctx, "oom", nil, 1024, time.Second)
		if err != nil {
			t.Fatal(err)
		}
	}
	if out.ExitCode == nil || *out.ExitCode != 137 {
		t.Fatalf("exit: %+v", out)
	}
	start("next")
	next := <-processes
	if running, err := sess.Terminate("next"); err != nil || !running {
		t.Fatalf("terminate: %v %v", running, err)
	}
	if got := <-next.signals; got != syscall.SIGKILL {
		t.Fatalf("signal: %v", got)
	}
	next.exit <- 137
}
