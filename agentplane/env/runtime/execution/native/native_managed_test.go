package native

import (
	"context"
	"io"
	"net/url"
	"os/exec"
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
