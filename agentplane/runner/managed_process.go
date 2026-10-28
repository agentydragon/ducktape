package runner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
)

// managedProcess exposes native streams while COSI owns launch, exit observation,
// and fencing. Protocol workers may wait on it without driving backend operations.
type managedProcess struct {
	l      *lifecycle
	id     string
	native *supervision.Process
}

func (r *Runner) launchProcess(ctx context.Context, owner string, supervisor *supervision.Supervisor, config supervision.ProcessConfig) (*managedProcess, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	native, err := supervisor.NewProcess(config)
	if err != nil {
		return nil, err
	}
	p := &managedProcess{l: r.lifecycle, id: uuid.NewString(), native: native}
	deadline, _ := ctx.Deadline()
	r.mu.Lock()
	desired := r.lifecycle.desiredState()
	// The owning controller already admitted the operation. Accepted setup and
	// bootstrap work must finish even if Close has since closed new admissions.
	// Its operation slot keeps shutdown from advancing while it requests intent.
	if err := ctx.Err(); err != nil {
		r.mu.Unlock()
		return nil, err
	}
	err = r.lifecycle.processes.requestNative(ctx, p.id, processDesiredStateSpec{
		Owner: owner, Generation: desired.Generation, StartDeadline: deadline,
		StopTimeout: stopGrace + 5*time.Second,
	}, func(startCtx context.Context) (*processInstance, error) {
		if err := native.Start(startCtx); err != nil {
			return nil, err
		}
		done := make(chan error, 1)
		r.lifecycle.workers.Add(1)
		go func() {
			defer r.lifecycle.workers.Done()
			_, err := native.Wait(r.ctx)
			done <- err
		}()
		return &processInstance{pid: native.PID(), done: done, stop: func(stopCtx context.Context) error {
			return native.Stop(stopCtx, stopGrace)
		}}, nil
	})
	r.mu.Unlock()
	if err != nil {
		return nil, err
	}
	err = r.lifecycle.waitFor(ctx, func() (bool, error) {
		status, found, err := lifecycleRead[processStatusSpec](ctx, r.lifecycle.state, p.id)
		if err != nil || !found {
			return false, err
		}
		if status.StartError != "" {
			return false, errors.New(status.StartError)
		}
		if status.Started {
			return true, nil
		}
		if status.Phase == processExited {
			return false, errors.New("native process launch was canceled")
		}
		return false, nil
	})
	if err != nil {
		// Cancellation ends admission, but cannot abandon an uncertain launch.
		// Teardown joins the start and fences any process it returns late.
		return nil, errors.Join(err, p.Stop(context.WithoutCancel(ctx)))
	}
	return p, nil
}

func (p *managedProcess) pointer() resource.Pointer {
	return resource.NewMetadata(lifecycleNamespace, processDesiredStateType, p.id, resource.VersionUndefined)
}

func (p *managedProcess) waitStopped(ctx context.Context) error {
	return p.l.waitFor(ctx, func() (bool, error) {
		status, found, err := lifecycleRead[processStatusSpec](ctx, p.l.state, p.id)
		if err != nil {
			return false, err
		}
		if found {
			if status.StopError != "" {
				return false, fmt.Errorf("native process cleanup failed: %s", status.StopError)
			}
			return status.Phase == processExited, nil
		}
		// Another observer may have released the completed intent already.
		_, present, err := lifecycleRead[processDesiredStateSpec](ctx, p.l.state, p.id)
		return !present, err
	})
}

func (p *managedProcess) release(ctx context.Context) error {
	err := p.l.state.TeardownAndDestroy(ctx, p.pointer(), state.WithTeardownAndDestroyOwner("runner-host"))
	if state.IsNotFoundError(err) {
		return nil
	}
	return err
}

// Stop tears down intent and waits for the controller's finalizer. It never
// retries a failed native fence or drops ownership after an uncertain stop.
func (p *managedProcess) Stop(ctx context.Context) error {
	_, err := p.l.state.Teardown(ctx, p.pointer(), state.WithTeardownOwner("runner-host"))
	if state.IsNotFoundError(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := p.waitStopped(ctx); err != nil {
		return err
	}
	return p.release(ctx)
}

// Wait includes the COSI cleanup barrier. A canceled observation leaves the
// intent and process owned; callers explicitly Stop when their policy requires it.
func (p *managedProcess) Wait(ctx context.Context) (int, error) {
	code, err := p.native.Wait(ctx)
	if ctx.Err() != nil {
		return code, err
	}
	if stopErr := p.waitStopped(ctx); stopErr != nil {
		return code, errors.Join(err, stopErr)
	}
	return code, errors.Join(err, p.release(ctx))
}

func (p *managedProcess) Running() bool     { return p.native.Running() }
func (p *managedProcess) PID() int          { return p.native.PID() }
func (p *managedProcess) Stdout() io.Reader { return p.native.Stdout() }
func (p *managedProcess) Stderr() io.Reader { return p.native.Stderr() }
func (p *managedProcess) Write(ctx context.Context, data []byte) error {
	return p.native.Write(ctx, data)
}

func (p *managedProcess) WriteLine(ctx context.Context, line string) error {
	return p.native.WriteLine(ctx, line)
}
func (p *managedProcess) CloseStdin() error          { return p.native.CloseStdin() }
func (p *managedProcess) ExitDescription() string    { return p.native.ExitDescription() }
func (p *managedProcess) BackendDescription() string { return p.native.BackendDescription() }
