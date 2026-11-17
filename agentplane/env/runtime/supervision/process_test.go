package supervision

import (
	"context"
	"io"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type recordedExitBackend struct{ handle *recordedExitHandle }

func (*recordedExitBackend) validate() error { return nil }

func (b *recordedExitBackend) start(context.Context) (processHandle, error) {
	return b.handle, nil
}

type recordedExitHandle struct {
	exitDone chan struct{}
	done     chan struct{}
	stopOnce sync.Once
}

func (*recordedExitHandle) PID() int                { return 23 }
func (h *recordedExitHandle) Done() <-chan struct{} { return h.done }
func (*recordedExitHandle) Stdin() *os.File         { return nil }
func (*recordedExitHandle) Stdout() io.Reader       { return strings.NewReader("") }
func (*recordedExitHandle) Stderr() io.Reader       { return strings.NewReader("") }
func (*recordedExitHandle) CloseStdin() error       { return nil }
func (h *recordedExitHandle) Stop(context.Context) error {
	h.stopOnce.Do(func() { close(h.done) })
	return nil
}

func (h *recordedExitHandle) Wait(ctx context.Context) (int, error) {
	select {
	case <-h.exitDone:
		return 23, nil
	default:
	}
	select {
	case <-h.exitDone:
		return 23, nil
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func TestNewProcessRejectsNegativeStopGrace(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command:   []string{"/bin/true"},
		StopGrace: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative stop grace")
	}
}

func TestNewProcessRejectsNegativeFenceTimeout(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command: []string{"/bin/true"}, FenceTimeout: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative fence timeout")
	}
}

func TestProcessWaitReturnsRecordedExitBeforeBackendCleanup(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	handle := &recordedExitHandle{exitDone: make(chan struct{}), done: make(chan struct{})}
	backend := &recordedExitBackend{handle: handle}
	supervisor := &Supervisor{makeBackend: func(ProcessConfig) processBackend { return backend }}
	process, err := supervisor.NewProcess(ProcessConfig{Command: []string{"native"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	// The backend has already recorded systemd's exit result, while its cleanup
	// fence and stream teardown are still pending.
	close(handle.exitDone)
	if code, err := process.Wait(ctx); err != nil || code != 23 {
		t.Fatalf("wait before cleanup = (%d, %v), want (23, nil)", code, err)
	}
	if !process.Running() {
		t.Fatal("recorded exit was mistaken for completed backend cleanup")
	}
	if err := process.Stop(ctx); err != nil {
		t.Fatalf("stop after recorded exit: %v", err)
	}
	if process.Running() {
		t.Fatal("Process.Stop returned before backend cleanup completed")
	}
}
