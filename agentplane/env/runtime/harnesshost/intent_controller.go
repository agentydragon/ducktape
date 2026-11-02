package harnesshost

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

// The Host owns application of its desired intent in the shared engine. A
// published Applying status is terminal on restart: effects are never replayed.
type hostIntentController struct {
	runner *Runner
	apply  func(context.Context, *runtimepb.LifecycleIntent) error
}

func (*hostIntentController) Name() string { return "harness-host-intent" }
func (*hostIntentController) Inputs() []controller.Input {
	return []controller.Input{{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.HostIntentType, Kind: controller.InputWeak}}
}

func (*hostIntentController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerlifecycle.HostIntentStatusType, Kind: controller.OutputExclusive}}
}

func (c *hostIntentController) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-engine.EventCh():
		}
		desired, found, err := cosi.Read(ctx, engine, runnerlifecycle.HostIntentResource(runnerlifecycle.HostIntent{}))
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		current, currentFound, err := cosi.Read(ctx, engine, runnerlifecycle.HostIntentStatusResource(runnerlifecycle.HostIntentStatus{}))
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if currentFound && !current.Applied && current.Error == "" {
			c.runner.fail(errors.New("prior Host lifecycle intent application has an unknown outcome"))
			return nil
		}
		if !shouldApplyHostIntent(found, desired, currentFound, current) {
			continue
		}
		if !c.eligible(desired) {
			continue
		}
		result := runnerlifecycle.HostIntentStatus{Intent: desired}
		if _, err := cosi.Write(ctx, engine, runnerlifecycle.HostIntentStatusResource(result), result); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if !c.eligible(desired) {
			result.Error = "Host connection ended before intent application"
			_, writeErr := cosi.Write(ctx, engine, runnerlifecycle.HostIntentStatusResource(result), result)
			c.runner.fail(fmt.Errorf("apply Host lifecycle intent: %w", errors.Join(errors.New(result.Error), writeErr)))
			return nil
		}
		apply := c.runner.ApplyIntent
		if c.apply != nil {
			apply = c.apply
		}
		applyErr := apply(ctx, desired.Proto())
		if applyErr != nil {
			result.Error = applyErr.Error()
		} else {
			result.Applied = true
		}
		_, writeErr := cosi.Write(ctx, engine, runnerlifecycle.HostIntentStatusResource(result), result)
		if applyErr != nil || writeErr != nil {
			c.runner.fail(fmt.Errorf("apply Host lifecycle intent: %w", errors.Join(applyErr, writeErr)))
		}
		engine.ResetRestartBackoff()
	}
}

func shouldApplyHostIntent(found bool, desired runnerlifecycle.HostIntent, currentFound bool, current runnerlifecycle.HostIntentStatus) bool {
	return found && (!currentFound || (current.Applied && current.Error == "" && current.Intent != desired))
}

func (c *hostIntentController) eligible(desired runnerlifecycle.HostIntent) bool {
	c.runner.mu.Lock()
	startup := c.runner.startupComplete && !c.runner.closing && c.runner.bootID == desired.HostBootID
	c.runner.mu.Unlock()
	return startup && c.runner.RuntimeConnectionMatches(desired.RuntimeBootID, desired.HostBootID)
}
