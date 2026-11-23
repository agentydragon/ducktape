package coordinator

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

// lifecycleGuard propagates COSI cancellation to Runtime's operation owner.
// The engine cancels its controllers before joining them and returning its
// error. A transition caller must be able to leave before those joins finish.
type lifecycleGuard struct{ runtime *Runtime }

func (*lifecycleGuard) Name() string { return "environment-runtime-lifecycle-guard" }

func (*lifecycleGuard) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputWeak},
	}
}

func (*lifecycleGuard) Outputs() []controller.Output { return nil }

func (c *lifecycleGuard) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	defer c.runtime.lifecycleCancel()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}
		if _, _, err := lifecycle.ReadEnvironmentDesired(ctx, r); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			c.runtime.mu.Lock()
			c.runtime.lifecycleControllerErr = err
			c.runtime.failLocked(fmt.Errorf("read Runtime desired state: %w", err))
			c.runtime.mu.Unlock()
			return err
		}
		r.ResetRestartBackoff()
	}
}
