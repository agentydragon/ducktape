package runtime

import (
	"context"
	"errors"
	"fmt"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func (r *Runtime) adoptIncarnation(ctx context.Context, uid string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if uid == "" {
		return errors.New("Runner incarnation UID is required")
	}
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	return r.requestTransition(ctx, runtimeAdopt, uid, nil)
}

func (r *Runtime) Quiesce(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	var idleSince *timestamppb.Timestamp
	if request != nil && request.IfIdleSince != nil {
		idleSince = proto.Clone(request.IfIdleSince).(*timestamppb.Timestamp)
	}
	if err := r.requestTransition(ctx, runtimeQuiesce, "", idleSince); err != nil {
		return nil, err
	}
	facts, err := r.facts()
	if err != nil {
		return nil, err
	}
	return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage(facts.QuiesceStage)}, nil
}

// transitionMu remains held until accepted work settles, including an Execution
// start whose caller has already been canceled. Close can cancel that work without
// admitting a successor before its exact fence completes.
func (r *Runtime) requestTransition(ctx context.Context, kind runtimeTransitionKind, uid string, idleSince *timestamppb.Timestamp) error {
	opCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "environment Runtime is closing")
	}
	if failure := r.failureLocked(); failure != nil {
		r.mu.Unlock()
		return failure
	}
	if r.transitionUnsafe != nil {
		unsafe := r.transitionUnsafe
		r.mu.Unlock()
		return fmt.Errorf("prior Runtime transition outcome is uncertain: %w", unsafe)
	}
	r.activeTransitionCancel = cancel
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.activeTransitionCancel = nil
		r.mu.Unlock()
	}()
	request := &runtimeTransitionRequest{ctx: opCtx, done: make(chan struct{}), Kind: kind, UID: uid}
	if idleSince != nil {
		request.HasIdleSince = true
		request.IdleSinceMicros = idleSince.AsTime().UnixMicro()
	}
	select {
	case r.operationCoordinator.requests <- request:
	case <-opCtx.Done():
		if err := ctx.Err(); err != nil {
			return err
		}
		return status.FromContextError(opCtx.Err()).Err()
	case <-r.operationCoordinator.stopped:
		return r.coordinatorFailure("transition admission")
	case <-r.lifecycleDone:
		return r.coordinatorFailure("transition admission")
	}
	select {
	case <-request.done:
		r.mu.Lock()
		unsafe := r.transitionUnsafe
		coordinatorErr := r.lifecycleControllerErr
		r.mu.Unlock()
		if coordinatorErr != nil {
			return coordinatorErr
		}
		if unsafe != nil {
			return unsafe
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		return request.err
	case <-r.operationCoordinator.stopped:
		return r.coordinatorFailure("transition completion")
	case <-r.lifecycleDone:
		return r.coordinatorFailure("transition completion")
	}
}

func (r *Runtime) coordinatorFailure(boundary string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.lifecycleControllerErr != nil {
		return r.lifecycleControllerErr
	}
	if failure := r.failureLocked(); failure != nil {
		return failure
	}
	return fmt.Errorf("Runtime lifecycle engine exited before %s", boundary)
}
