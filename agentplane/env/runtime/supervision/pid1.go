package supervision

import (
	"context"
	"errors"
	"fmt"
	"os"
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
	child  *pid1.Child
}

func (b *pid1Backend) validate() error { return nil }

func (b *pid1Backend) start(ctx context.Context, p *Process) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		return err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		return err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		return err
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
		return err
	}
	b.child = child
	p.setNativePID(child.PID())
	p.attachStreams(stdinWriter, stdoutReader, stderrReader)
	go b.finish(p)
	if err := ctx.Err(); err != nil {
		child.Kill()
		_, waitErr := p.Wait(context.Background())
		return errors.Join(err, waitErr)
	}
	return nil
}

func (b *pid1Backend) finish(p *Process) {
	exit, err := b.child.WaitReaped(context.Background())
	if err != nil {
		panic(fmt.Errorf("wait for PID1 native child %d: %w", b.child.PID(), err))
	}
	// Natural leader exit closes stdin before waiting for the complete process
	// group to disappear, then publishes Process completion.
	_ = p.CloseStdin()
	err = b.child.WaitFenced(context.Background())
	p.completeWith(exit.Code, err)
}

func (b *pid1Backend) stop(ctx context.Context, p *Process) error {
	if err := p.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	b.child.Terminate()
	timer := time.NewTimer(b.config.StopGrace)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-ctx.Done():
		b.child.Kill()
		return ctx.Err()
	case <-timer.C:
		b.child.Kill()
	}
	_, err := p.Wait(ctx)
	return err
}
