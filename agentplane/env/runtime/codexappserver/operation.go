package codexappserver

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
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
		func(bool) error {
			return c.updateLifecycleSignal(c.deps.Context, func(input *LifecycleSignal) { input.OperationRevision++ })
		},
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

func (c *Host) takeOperation(slot **oneoff.Operation) (*oneoff.Operation, bool) {
	return oneoff.Consume(&c.mu, slot)
}
