package supervision

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision/internal/pid1"
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

func TestPID1ChildWaitIsReplayableAndCancellationOnlyStopsObservation(t *testing.T) {
	reaper, err := pid1.Namespace()
	if err != nil {
		t.Fatal(err)
	}
	child, err := reaper.Start(pid1.Launch{Command: []string{"/bin/sleep", "60"}})
	if err != nil {
		t.Fatal(err)
	}
	cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancelCleanup()
	defer func() {
		child.Kill()
		_ = child.WaitFenced(cleanupCtx)
	}()

	waitCtx, cancelWait := context.WithCancel(context.Background())
	cancelWait()
	if _, err := child.WaitReaped(waitCtx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled reap observation = %v, want context.Canceled", err)
	}
	if err := unix.Kill(child.PID(), 0); err != nil {
		t.Fatalf("canceled observation released or stopped child %d: %v", child.PID(), err)
	}

	child.Kill()
	first, err := child.WaitReaped(cleanupCtx)
	if err != nil || first.Code != 137 || first.FenceError != nil {
		t.Fatalf("first reap result = (%+v, %v), want SIGKILL exit", first, err)
	}
	const waiters = 8
	results := make(chan pid1.Exit, waiters)
	errs := make(chan error, waiters)
	var group sync.WaitGroup
	for range waiters {
		group.Add(1)
		go func() {
			defer group.Done()
			result, err := child.WaitReaped(cleanupCtx)
			results <- result
			errs <- err
		}()
	}
	group.Wait()
	close(results)
	close(errs)
	for got := range results {
		if got != first {
			t.Fatalf("replayed result = %+v, want %+v", got, first)
		}
	}
	for err := range errs {
		if err != nil {
			t.Fatalf("replayed wait: %v", err)
		}
	}
	if err := child.WaitFenced(cleanupCtx); err != nil {
		t.Fatalf("wait for child fence: %v", err)
	}
}

func TestPID1ChildSignalAfterUnregistrationCannotAffectAnotherChild(t *testing.T) {
	reaper, err := pid1.Namespace()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	old, err := reaper.Start(pid1.Launch{Command: []string{"/bin/sh", "-c", "exit 0"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := old.WaitReaped(ctx); err != nil {
		t.Fatal(err)
	}
	if err := old.WaitFenced(ctx); err != nil {
		t.Fatal(err)
	}

	current, err := reaper.Start(pid1.Launch{Command: []string{"/bin/sleep", "60"}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		current.Kill()
		_ = current.WaitFenced(ctx)
	}()
	old.Kill()
	if err := unix.Kill(current.PID(), 0); err != nil {
		t.Fatalf("stale child signal affected current child %d: %v", current.PID(), err)
	}
	current.Kill()
	if err := current.WaitFenced(ctx); err != nil {
		t.Fatalf("wait for current child fence: %v", err)
	}
}

func TestPID1LaunchPreservesExplicitEmptyEnvironment(t *testing.T) {
	t.Setenv("PID1_ENV_LEAK_SENTINEL", "must-not-be-inherited")
	reaper, err := pid1.Namespace()
	if err != nil {
		t.Fatal(err)
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	child, err := reaper.Start(pid1.Launch{
		Command: []string{"/usr/bin/env"},
		Env:     []string{},
		Stdout:  writer,
	})
	_ = writer.Close()
	if err != nil {
		_ = reader.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if exit, err := child.WaitReaped(ctx); err != nil || exit.Code != 0 {
		t.Fatalf("env child exit = (%+v, %v)", exit, err)
	}
	if err := child.WaitFenced(ctx); err != nil {
		t.Fatalf("wait for env child fence: %v", err)
	}
	output, err := io.ReadAll(reader)
	_ = reader.Close()
	if err != nil {
		t.Fatal(err)
	}
	if len(output) != 0 {
		t.Fatalf("empty environment inherited parent values: %q", output)
	}
}

func TestPID1ProcessWaitCompletesAfterAdapterClosesStdin(t *testing.T) {
	supervisor, err := NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	process, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-c", "exit 42"}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if code, err := process.Wait(ctx); code != 42 || err != nil {
		t.Fatalf("process result = (%d, %v), want natural exit 42", code, err)
	}
	if _, err := process.stdin.Write([]byte("late")); !errors.Is(err, os.ErrClosed) {
		t.Fatalf("write to adapter stdin after process completion = %v", err)
	}
}
