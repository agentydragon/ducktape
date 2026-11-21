package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision/internal/pid1"
)

// NewPID1Supervisor requires PID 1 in a private container PID namespace whose
// runtime fences the namespace before replacement. PID 1 owns every wait4,
// including adopted orphans; exec.Cmd.Wait is never used.
func NewPID1Supervisor() (*Supervisor, error) {
	reaper, err := pid1.Namespace()
	if err != nil {
		return nil, err
	}
	return &Supervisor{makeBackend: func(config ProcessConfig) processBackend {
		return &pid1Backend{config: config, reaper: reaper}
	}, fenceNamespace: reaper.FenceNamespace}, nil
}

type pid1Backend struct {
	config ProcessConfig
	reaper *pid1.Reaper
}

func (b *pid1Backend) validate() error { return nil }

func (b *pid1Backend) start(ctx context.Context) (processHandle, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		return nil, err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		return nil, err
	}
	child, err := b.reaper.Start(pid1.Launch{
		Command: b.config.Command,
		Dir:     b.config.Dir,
		Env:     environment(b.config.Env),
		Stdin:   stdinReader,
		Stdout:  stdoutWriter,
		Stderr:  stderrWriter,
	})
	closeFiles(stdinReader, stdoutWriter, stderrWriter)
	if err != nil {
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return nil, err
	}
	handle := newPID1ProcessHandle(child, stdinWriter, stdoutReader, stderrReader, b.config.StopGrace)
	if err := ctx.Err(); err != nil {
		child.Kill()
		_, waitErr := handle.Wait(context.Background())
		return handle, errors.Join(err, waitErr)
	}
	return handle, nil
}

type pid1ProcessHandle struct {
	child     *pid1.Child
	stdin     *os.File
	stdout    *os.File
	stderr    *os.File
	stopGrace time.Duration
	closeOnce sync.Once
	closeErr  error
	waitDone  chan struct{}
	waitCode  int
	waitErr   error
}

func newPID1ProcessHandle(child *pid1.Child, stdin, stdout, stderr *os.File, stopGrace time.Duration) *pid1ProcessHandle {
	handle := &pid1ProcessHandle{
		child: child, stdin: stdin, stdout: stdout, stderr: stderr,
		stopGrace: stopGrace, waitDone: make(chan struct{}),
	}
	go handle.reap()
	return handle
}

func (h *pid1ProcessHandle) PID() int              { return h.child.PID() }
func (h *pid1ProcessHandle) Done() <-chan struct{} { return h.waitDone }
func (h *pid1ProcessHandle) Stdin() *os.File       { return h.stdin }
func (h *pid1ProcessHandle) Stdout() io.Reader     { return h.stdout }
func (h *pid1ProcessHandle) Stderr() io.Reader     { return h.stderr }

func (h *pid1ProcessHandle) CloseStdin() error {
	h.closeOnce.Do(func() { h.closeErr = h.stdin.Close() })
	return h.closeErr
}

func (h *pid1ProcessHandle) reap() {
	exit, err := h.child.WaitReaped(context.Background())
	if err != nil {
		panic(fmt.Errorf("wait for PID1 native child %d: %w", h.child.PID(), err))
	}
	// Natural leader exit closes stdin before waiting for the complete process
	// group to disappear, then publishes Process completion.
	_ = h.CloseStdin()
	err = h.child.WaitFenced(context.Background())
	h.waitCode, h.waitErr = exit.Code, err
	close(h.waitDone)
}

func (h *pid1ProcessHandle) Wait(ctx context.Context) (int, error) {
	select {
	case <-h.waitDone:
		return h.waitCode, h.waitErr
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (h *pid1ProcessHandle) Stop(ctx context.Context) error {
	select {
	case <-h.waitDone:
		_, err := h.Wait(ctx)
		return err
	default:
	}
	if err := h.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	h.child.Terminate()
	timer := time.NewTimer(h.stopGrace)
	defer timer.Stop()
	select {
	case <-h.waitDone:
		_, err := h.Wait(context.Background())
		return err
	case <-ctx.Done():
		h.child.Kill()
		return ctx.Err()
	case <-timer.C:
		h.child.Kill()
	}
	_, err := h.Wait(ctx)
	return err
}
