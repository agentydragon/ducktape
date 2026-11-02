package runtime

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type hostReportObserver struct{ runtime *Runtime }

func (*hostReportObserver) Name() string { return "environment-host-report-observer" }
func (*hostReportObserver) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.HostReportType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: hostIntentStatusType, Kind: controller.InputWeak},
	}
}
func (*hostReportObserver) Outputs() []controller.Output { return nil }
func (c *hostReportObserver) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-engine.EventCh():
		}
		r := c.runtime
		r.mu.Lock()
		report := r.hostReportLocked()
		if report != nil {
			r.changedLocked()
		}
		var fenceBinding *runtimepb.Binding
		if report != nil && report.Ready && report.NativeStopped && r.intent != nil && !r.intent.ExecutionFenced &&
			(r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING || r.intent.Mode == runtimepb.LifecycleIntent_CLOSING) {
			fenceBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		}
		var failure error
		if report != nil && report.Error != "" {
			failure = status.Errorf(codes.FailedPrecondition, "Harness Host rejected lifecycle generation %d: %s", report.Binding.Generation, report.Error)
		}
		r.mu.Unlock()
		if failure != nil {
			r.fail(failure)
			return nil
		}
		if fenceBinding != nil {
			r.maybeFenceExecution(fenceBinding)
		}
		engine.ResetRestartBackoff()
	}
}

// Caller holds Runtime.mu. The exact applied intent invalidates an old report
// even when ExecutionFenced changes inside the same generation.
func (r *Runtime) hostReportLocked() *runtimepb.HostReport {
	if r.lifecycleState == nil || r.intent == nil {
		return nil
	}
	current, found, err := cosi.Read(context.Background(), r.lifecycleState, lifecycle.HostReportResource(lifecycle.HostReport{}))
	if err != nil {
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = fmt.Errorf("read Host report: %w", err)
		}
		r.changedLocked()
		select {
		case r.fatal <- err:
		default:
		}
		return nil
	}
	if !found || !proto.Equal(current.Intent, r.intent) || current.Report == nil ||
		!proto.Equal(current.Report.Binding, r.intent.Binding) {
		return nil
	}
	applied, _, err := cosi.Read(context.Background(), r.lifecycleState, hostIntentStatusResource(hostIntentStatus{}))
	if err != nil {
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = fmt.Errorf("read Host intent status: %w", err)
		}
		r.changedLocked()
		select {
		case r.fatal <- err:
		default:
		}
		return nil
	}
	if !applied.Applied || applied.Intent != intentValue(r.intent) {
		return nil
	}
	return current.Report
}
