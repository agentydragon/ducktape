package runtime

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type quiesceBarrierKind uint8

const (
	quiesceSeal quiesceBarrierKind = iota + 1
	quiesceCheckHostStatus
	quiescePublishIntent
)

// The seal barrier snapshots the exact Host binding and current desired state.
// Later workers return revised plans to the controller, which installs them
// only after each worker completes.
type quiescePlan struct {
	request              lifecycle.RuntimeTransitionRequest
	factsHaveIdleSince   bool
	factsIdleSinceMicros int64
	uid                  string
	current              bool
	host                 *hostLink
	binding              *runtimepb.Binding
	currentReport        bool
	retry                bool
}

func (c *runtimeTransitionSequence) planQuiesce(request lifecycle.RuntimeTransitionRequest) (quiescePlan, []runtimeTransitionStep, error) {
	facts, err := c.r.facts()
	if err != nil {
		return quiescePlan{}, nil, err
	}
	plan := quiescePlan{request: request}
	if facts.IdleSince != nil {
		plan.factsHaveIdleSince = true
		plan.factsIdleSinceMicros = facts.IdleSince.AsTime().UnixMicro()
	}
	return plan, []runtimeTransitionStep{
		{stage: lifecycle.RuntimeTransitionQuiesceSealing, quiesce: quiesceSeal},
		{stage: lifecycle.RuntimeTransitionHostStatusChecking, quiesce: quiesceCheckHostStatus},
		{stage: lifecycle.RuntimeTransitionIntentPublishing, quiesce: quiescePublishIntent},
	}, nil
}

func (c *runtimeTransitionSequence) runQuiesceBarrier(ctx context.Context, kind quiesceBarrierKind) (runtimeTransitionOutcome, error) {
	r := c.r
	plan := c.quiesce
	switch kind {
	case quiesceSeal:
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			return runtimeTransitionOutcome{}, errors.New("environment Runtime is closing")
		}
		report := r.hostReportLocked()
		if !r.state.Lameduck() && plan.request.HasIdleSince &&
			(!plan.factsHaveIdleSince || plan.factsIdleSinceMicros != plan.request.IdleSinceMicros ||
				r.activeAdmissions != 0 || r.activeExecutionCalls != 0 || r.executionRecovering ||
				r.host == nil || report == nil || !report.Ready || r.intent == nil ||
				!proto.Equal(report.Binding, r.intent.Binding) || report.Facts == nil ||
				report.Facts.IdleSince == nil ||
				report.Facts.IdleSince.AsTime().UnixMicro() != plan.request.IdleSinceMicros) {
			return runtimeTransitionOutcome{}, status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
		}
		if r.host == nil || !r.host.connected {
			return runtimeTransitionOutcome{}, status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
		}
		if err := r.state.EnterLameduck(); err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "persist environment seal: %v", err)
		}
		var err error
		plan.uid, err = r.state.RequireIncarnationUID()
		if err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.FailedPrecondition, "%v", err)
		}
		plan.current = r.desired.Mode == lifecycle.Quiescing && r.desired.SealUID == plan.uid && r.intent != nil &&
			r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == plan.uid
		if plan.current {
			plan.host = r.host
			plan.binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			plan.currentReport = report != nil && report.Ready && proto.Equal(report.Binding, plan.binding)
		}
		plan.retry = !plan.current
		return runtimeTransitionOutcome{quiesce: &plan}, nil
	case quiesceCheckHostStatus:
		if !plan.currentReport {
			return runtimeTransitionOutcome{quiesce: &plan}, nil
		}
		bound, err := plan.host.client.BoundContext(ctx, plan.binding)
		if err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "authorize Harness Host quiesce status: %v", err)
		}
		hostStatus, err := plan.host.client.GetStatus(bound, &sessionpb.GetStatusRequest{})
		if err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "read Harness Host quiesce status: %v", err)
		}
		plan.retry = hostStatus.GetLifecycle().GetError() != ""
		return runtimeTransitionOutcome{quiesce: &plan}, nil
	case quiescePublishIntent:
		if !plan.retry {
			return runtimeTransitionOutcome{}, nil
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			return runtimeTransitionOutcome{}, errors.New("environment Runtime is closing")
		}
		if plan.current && (r.host != plan.host || r.intent == nil || !proto.Equal(r.intent.Binding, plan.binding) ||
			r.desired.Mode != lifecycle.Quiescing || r.desired.SealUID != plan.uid) {
			return runtimeTransitionOutcome{}, nil
		}
		desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, SealUID: plan.uid, Generation: r.desired.Generation + 1}
		if err := lifecycle.PublishEnvironmentDesired(context.Background(), r.lifecycleState, desired); err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "publish environment quiesce: %v", err)
		}
		r.desired = desired
		r.setIntentLocked(runtimepb.LifecycleIntent_QUIESCING, plan.uid, plan.uid, false)
		return runtimeTransitionOutcome{}, nil
	default:
		return runtimeTransitionOutcome{}, status.Errorf(codes.Internal, "unknown Runtime quiesce barrier %d", kind)
	}
}
