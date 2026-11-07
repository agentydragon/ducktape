package runtime

import (
	"context"

	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// PrepareSuspend closes only new admission. The controller must commit a
// dispatch-fencing attempt before calling it, and may checkpoint RAM only after
// this call succeeds. The gate is separate from lifecycle admissionOpen so a
// same-incarnation WatchState adoption cannot reopen it before the checkpoint.
func (r *Runtime) PrepareSuspend(ctx context.Context, request *environmentruntimepb.PrepareSuspendRequest) (*emptypb.Empty, error) {
	if request == nil || request.Generation <= 0 || request.IfIdleSince == nil || request.IfIdleSince.CheckValid() != nil {
		return nil, status.Error(codes.InvalidArgument, "positive generation and valid if_idle_since are required")
	}
	idleSinceMicros := request.IfIdleSince.AsTime().UnixMicro()
	err := r.control.Admit(ctx, func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		if epoch.IncarnationUID() != r.desired.IncarnationUID || r.closing || r.failed || !r.startupComplete || !r.admissionOpen || r.desired.Mode != lifecycle.Running {
			r.mu.Unlock()
			return status.Error(codes.Unavailable, "environment Runtime is unavailable for suspension")
		}
		if request.Generation < r.suspendGeneration {
			r.mu.Unlock()
			return status.Error(codes.Aborted, "suspension generation is stale")
		}
		if request.Generation == r.suspendGeneration {
			prepared := r.suspendPrepared && idleSinceMicros == r.suspendIdleSinceMicros
			r.mu.Unlock()
			if prepared {
				return nil
			}
			return status.Error(codes.Aborted, "suspension generation was released or changed")
		}
		if r.suspendPrepared {
			r.mu.Unlock()
			return status.Error(codes.Aborted, "another suspension is prepared")
		}
		r.mu.Unlock()
		// facts() enters r.mu and may read the lifecycle state. The epoch remains
		// current through this callback; a new admission still clears idleSince.
		facts, err := r.facts()
		if err != nil {
			return err
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		if epoch.IncarnationUID() != r.desired.IncarnationUID || r.closing || r.failed || !r.startupComplete || !r.admissionOpen || r.desired.Mode != lifecycle.Running {
			return status.Error(codes.Unavailable, "environment Runtime is unavailable for suspension")
		}
		if request.Generation == r.suspendGeneration && r.suspendPrepared && idleSinceMicros == r.suspendIdleSinceMicros {
			return nil
		}
		if request.Generation <= r.suspendGeneration || r.suspendPrepared {
			return status.Error(codes.Aborted, "suspension generation changed")
		}
		if facts.IdleSince == nil || facts.IdleSince.AsTime().UnixMicro() != idleSinceMicros || len(facts.AutosuspendBlockers) != 0 ||
			r.activeAdmissions != 0 || r.activeExecutionCalls != 0 || r.executionOwner.recovering() ||
			r.idleSince == nil || r.idleSince.AsTime().UnixMicro() != idleSinceMicros {
			return status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
		}
		r.suspendGeneration = request.Generation
		r.suspendIdleSinceMicros = idleSinceMicros
		r.suspendPrepared = true
		r.changedLocked()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &emptypb.Empty{}, nil
}

// ResumeAdmission is also the generation tombstone. A delayed PrepareSuspend
// for a released attempt cannot close admission after the guest resumes.
func (r *Runtime) ResumeAdmission(ctx context.Context, request *environmentruntimepb.ResumeAdmissionRequest) (*emptypb.Empty, error) {
	if request == nil || request.Generation <= 0 {
		return nil, status.Error(codes.InvalidArgument, "positive generation is required")
	}
	err := r.control.Admit(ctx, func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if epoch.IncarnationUID() != r.desired.IncarnationUID || r.closing || r.failed || !r.startupComplete {
			return status.Error(codes.Unavailable, "environment Runtime is unavailable for admission resume")
		}
		if request.Generation < r.suspendGeneration {
			return status.Error(codes.Aborted, "suspension generation is stale")
		}
		r.suspendGeneration = request.Generation
		r.suspendIdleSinceMicros = 0
		r.suspendPrepared = false
		// A resumed guest or aborted attempt starts a new idle period.
		r.idleSince = nil
		r.changedLocked()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &emptypb.Empty{}, nil
}
