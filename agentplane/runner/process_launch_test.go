package runner

import (
	"context"
	"errors"
	"io"
	"testing"
	"time"

	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

func TestManagedProcessWaitCleansUpAfterNaturalExit(t *testing.T) {
	r := newManagedProcessTestRunner(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	process, err := r.launchProcess(ctx, "managed-process-test", r.supervisor, supervision.ProcessConfig{
		Command: []string{"/bin/sh", "-c", "IFS= read -r _; printf stdout; printf stderr >&2; exit 7"},
		Dir:     t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	stopManagedProcessOnCleanup(t, process)
	if !process.Running() || process.PID() == 0 {
		t.Fatalf("launched process running=%t pid=%d", process.Running(), process.PID())
	}
	status, found, err := supervision.ReadStatus(ctx, r.lifecycle.state, runnerlifecycle.Namespace, process.ID())
	if err != nil || !found || status.Phase != supervision.Running || status.Owner != "managed-process-test" || status.PID != process.PID() {
		t.Fatalf("live process status = %+v found=%t err=%v", status, found, err)
	}
	assertManagedProcessFinalizer(t, ctx, r, process, true)

	if err := process.WriteLine(ctx, "exit"); err != nil {
		t.Fatal(err)
	}
	if err := process.CloseStdin(); err != nil {
		t.Fatal(err)
	}
	code, err := process.Wait(ctx)
	if err != nil || code != 7 {
		t.Fatalf("Wait() = (%d, %v), want (7, nil)", code, err)
	}
	stdout, err := io.ReadAll(process.Stdout())
	if err != nil || string(stdout) != "stdout" {
		t.Fatalf("stdout = %q, err=%v", stdout, err)
	}
	stderr, err := io.ReadAll(process.Stderr())
	if err != nil || string(stderr) != "stderr" {
		t.Fatalf("stderr = %q, err=%v", stderr, err)
	}
	assertManagedProcessAbsent(t, ctx, r, process)
}

func TestManagedProcessCanceledWaitRetainsOwnershipUntilStop(t *testing.T) {
	r := newManagedProcessTestRunner(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	process, err := r.launchProcess(ctx, "managed-process-test", r.supervisor, supervision.ProcessConfig{
		Command: []string{"/bin/sleep", "60"},
		Dir:     t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	stopManagedProcessOnCleanup(t, process)
	assertManagedProcessFinalizer(t, ctx, r, process, true)

	waitCtx, cancelWait := context.WithCancel(ctx)
	waitStarted := make(chan struct{})
	type waitResult struct {
		code int
		err  error
	}
	waitDone := make(chan waitResult, 1)
	go func() {
		close(waitStarted)
		code, err := process.Wait(waitCtx)
		waitDone <- waitResult{code: code, err: err}
	}()
	<-waitStarted
	cancelWait()
	select {
	case result := <-waitDone:
		if !errors.Is(result.err, context.Canceled) {
			t.Fatalf("canceled Wait() = (%d, %v), want context.Canceled", result.code, result.err)
		}
	case <-ctx.Done():
		t.Fatalf("Wait() did not observe cancellation: %v", ctx.Err())
	}

	if !process.Running() {
		t.Fatal("canceled Wait stopped the native process")
	}
	status, found, err := supervision.ReadStatus(ctx, r.lifecycle.state, runnerlifecycle.Namespace, process.ID())
	if err != nil || !found || status.Phase != supervision.Running {
		t.Fatalf("process status after canceled Wait = %+v found=%t err=%v", status, found, err)
	}
	assertManagedProcessFinalizer(t, ctx, r, process, true)

	if err := process.Stop(ctx); err != nil {
		t.Fatalf("Stop() after canceled Wait: %v", err)
	}
	if process.Running() {
		t.Fatal("process remained running after Stop")
	}
	assertManagedProcessAbsent(t, ctx, r, process)
}

func TestManagedProcessBlocksCloseUntilFence(t *testing.T) {
	r := newManagedProcessTestRunner(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	process, err := r.launchProcess(ctx, "managed-process-test", r.supervisor, supervision.ProcessConfig{
		Command: []string{"/bin/sleep", "60"},
		Dir:     t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}

	closeDone := make(chan struct{})
	var closeErr error
	go func() {
		closeErr = r.Close(context.Background())
		close(closeDone)
	}()
	t.Cleanup(func() {
		select {
		case <-closeDone:
		case <-time.After(lifecycleTestTimeout):
			r.cancel()
			<-closeDone
			t.Errorf("Runner Close did not finish after process cleanup")
		}
		if closeErr != nil {
			t.Errorf("close managed process test runner: %v", closeErr)
		}
	})
	stopManagedProcessOnCleanup(t, process)

	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage >= runnerlifecycle.Fencing }, "Runner process fencing barrier")
	select {
	case <-closeDone:
		t.Fatalf("Runner Close returned before process fencing: %v", closeErr)
	default:
	}
	if !process.Running() {
		t.Fatal("native process stopped before explicit fencing")
	}

	if err := process.Stop(ctx); err != nil {
		t.Fatalf("fence managed process: %v", err)
	}
	select {
	case <-closeDone:
		if closeErr != nil {
			t.Fatalf("Runner Close after process fencing: %v", closeErr)
		}
	case <-ctx.Done():
		t.Fatalf("Runner Close did not finish after process fencing: %v", ctx.Err())
	}
}

func newManagedProcessTestRunner(t *testing.T) *Runner {
	t.Helper()
	r, err := New(Config{StateDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.AdoptIncarnation("managed-process-test"); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(context.Background()); err != nil {
			t.Errorf("close managed process test runner: %v", err)
		}
	})
	return r
}

func assertManagedProcessFinalizer(t *testing.T, ctx context.Context, r *Runner, process *supervision.ManagedProcess, want bool) {
	t.Helper()
	desired, err := r.lifecycle.state.Get(ctx, process.Pointer())
	if err != nil {
		t.Fatalf("read process desired state: %v", err)
	}
	got := desired.Metadata().Finalizers().Has(supervision.ControllerName)
	if got != want {
		t.Fatalf("process finalizer present=%t, want %t", got, want)
	}
}

func assertManagedProcessAbsent(t *testing.T, ctx context.Context, r *Runner, process *supervision.ManagedProcess) {
	t.Helper()
	_, err := r.lifecycle.state.Get(ctx, process.Pointer())
	if !state.IsNotFoundError(err) {
		t.Fatalf("process desired state after cleanup: err=%v, want not found", err)
	}
	status := resource.NewMetadata(runnerlifecycle.Namespace, supervision.StatusType, process.ID(), resource.VersionUndefined)
	if _, err := r.lifecycle.state.WatchFor(ctx, status, state.WithEventTypes(state.Destroyed)); err != nil {
		t.Fatalf("wait for managed process status cleanup: %v", err)
	}
}

func stopManagedProcessOnCleanup(t *testing.T, process *supervision.ManagedProcess) {
	t.Helper()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer cancel()
		if err := process.Stop(ctx); err != nil {
			t.Errorf("stop managed process during cleanup: %v", err)
		}
	})
}
