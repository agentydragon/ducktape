package coordinator

import (
	"context"
	"errors"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// runtimeOperationCoordinator owns accepted adoption and Quiesce operations for
// one Runtime process. It is not a COSI controller: its request and worker slots
// are live Go ownership, and restarting a controller must never replay an effect.
type runtimeOperationCoordinator struct {
	r        *Runtime
	requests chan *runtimeTransitionRequest
	stopped  chan struct{}
	done     chan struct{}
}

type runtimeOperationWorker struct {
	request *runtimeTransitionRequest
	done    chan struct{}
	cancel  context.CancelFunc
	err     error
}

func (c *runtimeOperationCoordinator) run(ctx context.Context) {
	defer close(c.done)
	var worker *runtimeOperationWorker
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
		if worker != nil {
			worker.cancel()
			<-worker.done
		}
	}()
	for {
		var workerDone <-chan struct{}
		if worker != nil {
			workerDone = worker.done
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
			if worker != nil {
				c.r.fail(errors.New("Runtime transition superseded before settlement"))
				return
			}
			opCtx, cancel := context.WithCancel(request.ctx)
			accepted := &runtimeOperationWorker{request: request, done: make(chan struct{}), cancel: cancel}
			worker = accepted
			go func() {
				accepted.err = c.execute(opCtx, ctx, request)
				close(accepted.done)
			}()
		case <-workerDone:
			if ctx.Err() != nil {
				return
			}
			worker.cancel()
			err := worker.err
			if errors.Is(err, context.Canceled) {
				err = status.Error(codes.Canceled, err.Error())
			} else if errors.Is(err, context.DeadlineExceeded) {
				err = status.Error(codes.DeadlineExceeded, err.Error())
			}
			worker.request.err = err
			close(worker.request.done)
			worker = nil
		}
	}
}

func (c *runtimeOperationCoordinator) execute(ctx, engineCtx context.Context, request *runtimeTransitionRequest) error {
	switch request.Kind {
	case runtimeAdopt:
		return c.r.runAdoption(ctx, engineCtx, request.UID)
	case runtimeQuiesce:
		return c.r.runQuiesce(ctx, engineCtx, request)
	default:
		return fmt.Errorf("unknown Runtime transition kind %d", request.Kind)
	}
}

// Check both owners between effects: the caller may cancel independently of the
// engine, and an engine exit must stop the sequence before its worker is joined.
func transitionContextError(ctx, engineCtx context.Context) error {
	if err := engineCtx.Err(); err != nil {
		return err
	}
	return ctx.Err()
}
