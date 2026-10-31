package codexappserver

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

func (c *Host) startOperation(slot **oneoff.Operation, work func(context.Context) error) error {
	c.mu.Lock()
	if *slot != nil {
		c.mu.Unlock()
		return nil
	}
	op := oneoff.New(c.deps.Context)
	op.Register(c.deps.Workers)
	*slot = op
	c.mu.Unlock()
	if err := op.Start(
		&c.mu,
		func(done bool) error { return c.publishOperation(op, done) },
		c.deps.OnError,
		work,
		c.notify,
		c.notify,
	); err != nil {
		c.mu.Lock()
		if *slot == op {
			*slot = nil
		}
		c.mu.Unlock()
		return err
	}
	return nil
}

func (c *Host) publishOperation(op *oneoff.Operation, done bool) error {
	return publish(c.deps.Context, c.deps.State, op.ID(), operationResult{Done: done})
}

func (c *Host) forgetOperation(op *oneoff.Operation) error {
	err := c.deps.State.Destroy(c.deps.Context, resource.NewMetadata(lifecycle.Namespace, OperationType, op.ID(), resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if state.IsNotFoundError(err) {
		return nil
	}
	return err
}

func (c *Host) takeOperation(slot **oneoff.Operation) (*oneoff.Operation, bool) {
	op, done := oneoff.Consume(&c.mu, slot)
	if !done {
		return nil, false
	}
	if err := c.forgetOperation(op); err != nil {
		c.deps.OnError(err)
	}
	return op, true
}
