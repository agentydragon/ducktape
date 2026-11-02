package harnesshost

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
	"google.golang.org/protobuf/proto"
)

type hostReportController struct{ runner *Runner }

func (*hostReportController) Name() string { return "harness-host-report" }
func (*hostReportController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.DesiredStateType, Kind: controller.InputWeak},
		{Namespace: runnerlifecycle.Namespace, Type: runnerlifecycle.AggregateStatusType, Kind: controller.InputWeak},
	}
}

func (*hostReportController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerlifecycle.HostReportType, Kind: controller.OutputExclusive}}
}

func (c *hostReportController) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		changed := c.runner.changesSince()
		intent, report, err := c.runner.reportSnapshot(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			c.runner.fail(err)
			return nil
		}
		if intent != nil && report != nil {
			value := runnerlifecycle.HostReport{Intent: intent, Report: report}
			previous, found, err := cosi.Read(ctx, engine, runnerlifecycle.HostReportResource(runnerlifecycle.HostReport{}))
			if err != nil {
				return err
			}
			if !found || !proto.Equal(previous.Intent, intent) || !proto.Equal(previous.Report, report) {
				if _, err := cosi.Write(ctx, engine, runnerlifecycle.HostReportResource(value), value); err != nil {
					return err
				}
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-engine.EventCh():
		case <-changed:
		}
		engine.ResetRestartBackoff()
	}
}

// Capture the complete intent on both sides of mutable inventory sampling.
// In particular, an old same-generation report cannot be tagged with a new fence.
func (r *Runner) reportSnapshot(ctx context.Context) (*runtimepb.LifecycleIntent, *runtimepb.HostReport, error) {
	r.mu.Lock()
	var sampled *runtimepb.LifecycleIntent
	if r.intent != nil {
		sampled = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	}
	r.mu.Unlock()
	if sampled == nil {
		return nil, nil, nil
	}
	facts, err := r.factsWithContext(ctx)
	if err != nil {
		return nil, nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if !proto.Equal(sampled, r.intent) {
		return nil, nil, nil
	}
	report := &runtimepb.HostReport{Binding: proto.Clone(sampled.Binding).(*runtimepb.Binding), Ready: r.ready.Load(), NativeStopped: r.nativeStopped, Facts: facts}
	current, _, err := runnerlifecycle.ReadStatus(ctx, r.lifecycle.state)
	if err != nil {
		return nil, nil, err
	}
	if current.Generation == sampled.Binding.Generation {
		report.Error = current.Error
	}
	return sampled, report, nil
}
