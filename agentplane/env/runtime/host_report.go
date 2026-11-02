package runtime

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"go.uber.org/zap"
	"google.golang.org/protobuf/proto"
)

const hostReportType = "HostReports.agentplane"

type hostReport struct {
	Intent hostIntent
	Report *runtimepb.HostReport
}

func (v hostReport) DeepCopy() hostReport {
	if v.Report != nil {
		v.Report = proto.Clone(v.Report).(*runtimepb.HostReport)
	}
	return v
}

type hostReportExtension struct{}

func (hostReportExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: hostReportType, DefaultNamespace: lifecycle.Namespace}
}

func hostReportResource(v hostReport) *typed.Resource[hostReport, hostReportExtension] {
	return typed.NewResource[hostReport, hostReportExtension](resource.NewMetadata(lifecycle.Namespace, hostReportType, "host", resource.VersionUndefined), v)
}

type hostReportController struct{ runtime *Runtime }

func (*hostReportController) Name() string { return "environment-host-report" }
func (*hostReportController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: hostIntentType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: hostIntentStatusType, Kind: controller.InputWeak},
	}
}

func (*hostReportController) Outputs() []controller.Output {
	return []controller.Output{{Type: hostReportType, Kind: controller.OutputExclusive}}
}

func (c *hostReportController) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		r := c.runtime
		r.mu.Lock()
		host := r.host
		connected := host != nil && host.connected
		r.mu.Unlock()
		var changed <-chan struct{}
		if connected {
			// Capture the notification before the snapshot so a concurrent Host update
			// cannot be lost. Native Session activity also wakes this controller.
			changed = host.peer.ChangesSince()
			sampled, _, err := cosi.Read(ctx, engine, hostIntentStatusResource(hostIntentStatus{}))
			if err != nil {
				return err
			}
			report, err := host.peer.Report()
			if err != nil {
				r.fail(fmt.Errorf("observe Host lifecycle report: %w", err))
				return nil
			}
			if sampled.Applied && report != nil {
				// Applied-intent updates must re-observe even a byte-identical report:
				// publishing the same-generation fence invalidates Runtime's old sample.
				if err := r.acceptReport(host, report, sampled.Intent); err != nil {
					r.fail(err)
					return nil
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

// Caller holds Runtime.mu. Binding validation also invalidates the old report
// when the Execution fence advances within the same generation.
func (r *Runtime) hostReportLocked() *runtimepb.HostReport {
	if r.lifecycleState == nil || r.intent == nil {
		return nil
	}
	current, _, err := cosi.Read(context.Background(), r.lifecycleState, hostReportResource(hostReport{}))
	if err != nil {
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = err
		}
		r.changedLocked()
		select {
		case r.fatal <- err:
		default:
		}
		return nil
	}
	if current.Intent != intentValue(r.intent) {
		return nil
	}
	return current.Report
}
