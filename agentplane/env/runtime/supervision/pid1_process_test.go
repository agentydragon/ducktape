package supervision

import (
	"bufio"
	"context"
	"errors"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestSupervisorProcessesHaveIndependentLifetimes(t *testing.T) {
	supervisor, err := NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var processes []*Process
	for range 2 {
		process, err := supervisor.NewProcess(ProcessConfig{
			Command:   []string{"/bin/sh", "-c", `while IFS= read -r line; do printf '%s\n' "$line"; done`},
			StopGrace: 0,
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := process.Start(ctx); err != nil {
			t.Fatal(err)
		}
		processes = append(processes, process)
		t.Cleanup(func() {
			if err := process.Stop(context.Background()); err != nil {
				t.Error(err)
			}
		})
	}
	if err := processes[0].Stop(ctx); err != nil {
		t.Fatal(err)
	}
	if err := processes[1].WriteLine(ctx, "still running"); err != nil {
		t.Fatal(err)
	}
	type reply struct {
		line string
		err  error
	}
	replies := make(chan reply, 1)
	go func() {
		line, err := bufio.NewReader(processes[1].Stdout()).ReadString('\n')
		replies <- reply{line, err}
	}()
	select {
	case got := <-replies:
		if got.err != nil || got.line != "still running\n" {
			t.Fatalf("second process after stopping first: %q, %v", got.line, got.err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}

func TestInputCancellationAndCloseUnblockPendingWrites(t *testing.T) {
	supervisor, err := NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	process, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/bin/sleep", "60"}, StopGrace: 0})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancelCleanup()
	defer func() {
		if pid := process.PID(); pid != 0 {
			_ = unix.Kill(-pid, unix.SIGKILL)
		}
		_, _ = process.Wait(cleanupCtx)
	}()
	writeDone := make(chan error, 1)
	writeCtx, cancelWrite := context.WithCancel(context.Background())
	defer cancelWrite()
	go func() {
		writeDone <- process.Write(writeCtx, make([]byte, 8*1024*1024))
	}()
	// The harness never reads stdin, so this write must block once the pipe fills.
	time.Sleep(50 * time.Millisecond)
	cancelWrite()
	select {
	case err := <-writeDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("pending write after cancellation = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("pending input write did not observe context cancellation")
	}
	go func() {
		writeDone <- process.Write(context.Background(), make([]byte, 8*1024*1024))
	}()
	select {
	case err := <-writeDone:
		t.Fatalf("previous cancellation prevented a new write: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	closed := make(chan error, 1)
	go func() { closed <- process.CloseStdin() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("CloseStdin: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("CloseStdin was blocked behind a stalled input write")
	}
	select {
	case err := <-writeDone:
		if err == nil {
			t.Fatal("pending write unexpectedly succeeded after stdin was closed")
		}
	case <-time.After(time.Second):
		t.Fatal("pending input write did not observe stdin closure")
	}
	if err := process.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}
