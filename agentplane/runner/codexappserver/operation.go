package codexappserver

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
)

// A slot stays owned until its controller consumes the published result.
type operation struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func (c *Host) startOperation(slot **operation, work func(context.Context) error) error {
	c.mu.Lock()
	if *slot != nil {
		c.mu.Unlock()
		return nil
	}
	ctx, cancel := context.WithCancel(c.deps.Context)
	op := &operation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	*slot = op
	c.deps.Workers.Add(1)
	c.mu.Unlock()
	if err := publish(c.deps.Context, c.deps.State, op.id, operationResult{}); err != nil {
		c.mu.Lock()
		if *slot == op {
			*slot = nil
		}
		c.mu.Unlock()
		cancel()
		c.deps.Workers.Done()
		return err
	}
	c.notify()
	go func() {
		defer c.deps.Workers.Done()
		defer cancel()
		err := work(ctx)
		c.mu.Lock()
		op.err = err
		if publishErr := publish(c.deps.Context, c.deps.State, op.id, operationResult{Done: true}); publishErr != nil {
			c.deps.OnError(publishErr)
			c.mu.Unlock()
			return
		}
		close(op.done)
		c.mu.Unlock()
		c.notify()
	}()
	return nil
}

func (c *Host) takeOperation(slot **operation) (*operation, bool) {
	c.mu.Lock()
	op := *slot
	if op == nil {
		c.mu.Unlock()
		return nil, false
	}
	select {
	case <-op.done:
		*slot = nil
	default:
		c.mu.Unlock()
		return nil, false
	}
	c.mu.Unlock()
	err := c.deps.State.Destroy(c.deps.Context, resource.NewMetadata(lifecycle.Namespace, OperationType, op.id, resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if err != nil && !state.IsNotFoundError(err) {
		c.deps.OnError(err)
	}
	return op, true
}
