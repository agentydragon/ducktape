package runtime

import (
	"context"
	"errors"
	"fmt"
)

// runtimeOperationCoordinator owns accepted adoption and Quiesce operations for
// one Runtime process. It is not a COSI controller: its request and worker slots
// are live Go ownership, and restarting a controller must never replay an effect.
type runtimeOperationCoordinator struct {
	r          *Runtime
	transition runtimeTransitionSequence
	requests   chan *runtimeTransitionRequest
	stopped    chan struct{}
	done       chan struct{}
}

func (c *runtimeOperationCoordinator) run(ctx context.Context) {
	defer close(c.done)
	c.transition.engineCtx = ctx
	defer func() {
		// A caller holds transitionMu until its accepted operation settles. Tell
		// it the owner stopped before joining an effect worker; Close must be
		// able to take that mutex even if the worker is still unwinding.
		c.r.mu.Lock()
		if c.r.activeTransitionCancel != nil && c.r.lifecycleControllerErr == nil {
			c.r.lifecycleControllerErr = errors.New("Runtime operation coordinator exited before active work settled")
			c.r.changedLocked()
		}
		c.r.mu.Unlock()
		close(c.stopped)
		if c.transition.cancel != nil {
			c.transition.cancel()
		}
		if c.transition.worker != nil {
			<-c.transition.worker.done
		}
	}()
	for {
		var workerDone <-chan struct{}
		if c.transition.worker != nil {
			workerDone = c.transition.worker.done
		}
		select {
		case <-ctx.Done():
			return
		case request := <-c.requests:
			if err := ctx.Err(); err != nil {
				request.err = fmt.Errorf("Runtime operation coordinator stopped before admission: %w", err)
				close(request.done)
				return
			}
			if err := c.transition.begin(request); err != nil {
				c.r.fail(err)
				return
			}
		case <-workerDone:
		}
		// A worker completion and owner cancellation can arrive together.
		// Do not start the next effect after COSI ownership has ended.
		if ctx.Err() != nil {
			return
		}
		c.transition.reconcile()
	}
}
