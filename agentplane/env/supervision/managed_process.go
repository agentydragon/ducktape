package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
)

// ManagedProcess exposes native streams while COSI owns launch, exit observation,
// and fencing. Protocol workers may wait on it without driving backend operations.
type ManagedProcess struct {
	host   *Host
	id     string
	native *Process
}

// Launch publishes a native process attempt and waits for its start observation.
// Admission and restart policy remain with the caller.
func (h *Host) Launch(ctx context.Context, owner string, generation uint64, supervisor *Supervisor, config ProcessConfig) (*ManagedProcess, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := h.ctx.Err(); err != nil {
		return nil, err
	}
	native, err := supervisor.NewProcess(config)
	if err != nil {
		return nil, err
	}
	p := &ManagedProcess{host: h, id: uuid.NewString(), native: native}
	deadline, _ := ctx.Deadline()
	err = h.requestNative(ctx, p.id, DesiredState{
		Owner: owner, Generation: generation, StartDeadline: deadline, StopTimeout: stopGrace + 5*time.Second,
	}, func(startCtx context.Context) (*Instance, error) {
		if err := native.Start(startCtx); err != nil {
			return nil, err
		}
		done := make(chan error, 1)
		h.workers.Add(1)
		go func() {
			defer h.workers.Done()
			_, err := native.Wait(h.ctx)
			done <- err
		}()
		return &Instance{PID: native.PID(), Done: done, Stop: func(stopCtx context.Context) error { return native.Stop(stopCtx, stopGrace) }}, nil
	})
	if err != nil {
		return nil, err
	}
	err = h.waitForStatus(ctx, p.id, func(status Status, found bool) (bool, error) {
		if !found {
			return false, nil
		}
		if status.StartError != "" {
			return false, errors.New(status.StartError)
		}
		if status.Started {
			return true, nil
		}
		if status.Phase == Exited {
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

// Pointer identifies the desired-state resource for this launch.
func (p *ManagedProcess) Pointer() resource.Pointer {
	return resource.NewMetadata(p.host.namespace, DesiredStateType, p.id, resource.VersionUndefined)
}

// WaitStopped observes completed cleanup without requesting a stop.
func (p *ManagedProcess) WaitStopped(ctx context.Context) error {
	return p.host.waitForStatus(ctx, p.id, func(status Status, found bool) (bool, error) {
		if found {
			if status.StopError != "" {
				return false, fmt.Errorf("native process cleanup failed: %s", status.StopError)
			}
			return status.Phase == Exited, nil
		}
		_, present, err := ReadDesiredState(ctx, p.host.state, p.host.namespace, p.id)
		return !present, err
	})
}

// Status waits use resource watches, independently of the embedding application.
func (h *Host) waitForStatus(ctx context.Context, id string, condition func(Status, bool) (bool, error)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(h.ctx, cancel)
	defer stop()
	ptr := resource.NewMetadata(h.namespace, StatusType, id, resource.VersionUndefined)
	_, err := h.state.WatchFor(ctx, ptr, state.WithEventTypes(state.Created, state.Updated, state.Destroyed), state.WithCondition(func(resource.Resource) (bool, error) {
		status, found, err := ReadStatus(ctx, h.state, h.namespace, id)
		if err != nil {
			return false, err
		}
		return condition(status, found)
	}))
	return err
}

func (p *ManagedProcess) release(ctx context.Context) error {
	err := p.host.state.TeardownAndDestroy(ctx, p.Pointer(), state.WithTeardownAndDestroyOwner(hostOwner))
	if state.IsNotFoundError(err) {
		return nil
	}
	return err
}

// Stop tears down intent and waits for the controller's finalizer. It never
// retries a failed native fence or drops ownership after an uncertain stop.
func (p *ManagedProcess) Stop(ctx context.Context) error {
	_, err := p.host.state.Teardown(ctx, p.Pointer(), state.WithTeardownOwner(hostOwner))
	if state.IsNotFoundError(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := p.WaitStopped(ctx); err != nil {
		return err
	}
	return p.release(ctx)
}

// Wait includes the COSI cleanup barrier. A canceled observation leaves the
// intent and process owned; callers explicitly Stop when their policy requires it.
func (p *ManagedProcess) Wait(ctx context.Context) (int, error) {
	code, err := p.native.Wait(ctx)
	if ctx.Err() != nil {
		return code, err
	}
	if stopErr := p.WaitStopped(ctx); stopErr != nil {
		return code, errors.Join(err, stopErr)
	}
	return code, errors.Join(err, p.release(ctx))
}

func (p *ManagedProcess) Running() bool     { return p.native.Running() }
func (p *ManagedProcess) PID() int          { return p.native.PID() }
func (p *ManagedProcess) Stdout() io.Reader { return p.native.Stdout() }
func (p *ManagedProcess) Stderr() io.Reader { return p.native.Stderr() }
func (p *ManagedProcess) Write(ctx context.Context, data []byte) error {
	return p.native.Write(ctx, data)
}

func (p *ManagedProcess) WriteLine(ctx context.Context, line string) error {
	return p.native.WriteLine(ctx, line)
}
func (p *ManagedProcess) CloseStdin() error          { return p.native.CloseStdin() }
func (p *ManagedProcess) ExitDescription() string    { return p.native.ExitDescription() }
func (p *ManagedProcess) BackendDescription() string { return p.native.BackendDescription() }

// ID is the immutable identity of this native launch.
func (p *ManagedProcess) ID() string { return p.id }
