package harnesshost

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

// The Host owns application of its desired intent in the shared engine. A
// published Applying status is terminal on restart: effects are never replayed.
type hostIntentController struct {
	host  *Host
	apply func(context.Context, *runtimepb.LifecycleIntent) error
}

func (*hostIntentController) Name() string { return "harness-host-intent" }
func (*hostIntentController) Inputs() []controller.Input {
	return []controller.Input{{Namespace: harnesshostlifecycle.Namespace, Type: harnesshostlifecycle.HostIntentType, Kind: controller.InputWeak}}
}

func (*hostIntentController) Outputs() []controller.Output {
	return []controller.Output{{Type: harnesshostlifecycle.HostIntentStatusType, Kind: controller.OutputExclusive}}
}

func (c *hostIntentController) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-engine.EventCh():
		}
		desired, found, err := cosi.Read(ctx, engine, harnesshostlifecycle.HostIntentResource(harnesshostlifecycle.HostIntent{}))
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		current, currentFound, err := cosi.Read(ctx, engine, harnesshostlifecycle.HostIntentStatusResource(harnesshostlifecycle.HostIntentStatus{}))
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if currentFound && !current.Applied && current.Error == "" {
			c.host.fail(errors.New("prior Host lifecycle intent application has an unknown outcome"))
			return nil
		}
		if !shouldApplyHostIntent(found, desired, currentFound, current) {
			continue
		}
		if !c.eligible(desired) {
			continue
		}
		result := harnesshostlifecycle.HostIntentStatus{Intent: desired}
		if err := harnesshostlifecycle.WriteHostIntentStatus(ctx, engine, result); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if !c.eligible(desired) {
			result.Error = "Host connection ended before intent application"
			writeErr := harnesshostlifecycle.WriteHostIntentStatus(ctx, engine, result)
			c.host.fail(fmt.Errorf("apply Host lifecycle intent: %w", errors.Join(errors.New(result.Error), writeErr)))
			return nil
		}
		apply := c.host.ApplyIntent
		if c.apply != nil {
			apply = c.apply
		}
		applyErr := apply(ctx, desired.Proto())
		if applyErr != nil {
			result.Error = applyErr.Error()
		} else {
			result.Applied = true
		}
		writeErr := harnesshostlifecycle.WriteHostIntentStatus(ctx, engine, result)
		if applyErr != nil || writeErr != nil {
			c.host.fail(fmt.Errorf("apply Host lifecycle intent: %w", errors.Join(applyErr, writeErr)))
		}
		engine.ResetRestartBackoff()
	}
}

func shouldApplyHostIntent(found bool, desired harnesshostlifecycle.HostIntent, currentFound bool, current harnesshostlifecycle.HostIntentStatus) bool {
	return found && (!currentFound || (current.Applied && current.Error == "" && current.Intent != desired))
}

func (c *hostIntentController) eligible(desired harnesshostlifecycle.HostIntent) bool {
	c.host.mu.Lock()
	startup := c.host.startupComplete && !c.host.closing && c.host.bootID == desired.HostBootID
	c.host.mu.Unlock()
	return startup && c.host.RuntimeConnectionMatches(desired.RuntimeBootID, desired.HostBootID)
}
