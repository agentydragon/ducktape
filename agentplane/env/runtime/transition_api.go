package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
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
	return r.requestTransition(ctx, lifecycle.RuntimeAdopt, uid, nil)
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
	if err := r.requestTransition(ctx, lifecycle.RuntimeQuiesce, "", idleSince); err != nil {
		return nil, err
	}
	facts, err := r.facts()
	if err != nil {
		return nil, err
	}
	return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage(facts.QuiesceStage)}, nil
}

// The Gateway holds transitionMu until the controller has published the exact
// terminal status. Epoch cancellation cancels future adoption stages but does
// not release a successor while an accepted Execution start remains unsettled.
func (r *Runtime) requestTransition(ctx context.Context, kind lifecycle.RuntimeTransitionKind, uid string, idleSince *timestamppb.Timestamp) error {
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
	r.activeTransitionSequence++
	sequence := r.activeTransitionSequence
	r.activeTransitionCtx = opCtx
	r.activeTransitionCancel = cancel
	transition := lifecycle.RuntimeTransitionRequest{Sequence: sequence, Kind: kind, UID: uid}
	if idleSince != nil {
		transition.HasIdleSince = true
		transition.IdleSinceMicros = idleSince.AsTime().UnixMicro()
	}
	err := lifecycle.PublishRuntimeTransitionRequest(context.Background(), r.lifecycleState, transition)
	if err != nil {
		r.activeTransitionCtx, r.activeTransitionCancel = nil, nil
		r.mu.Unlock()
		return fmt.Errorf("publish Runtime transition: %w", err)
	}
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		if r.activeTransitionSequence == sequence {
			r.activeTransitionCtx, r.activeTransitionCancel = nil, nil
		}
		r.mu.Unlock()
	}()
	for {
		r.mu.Lock()
		changed := r.changes
		controllerErr := r.lifecycleControllerErr
		r.mu.Unlock()
		if controllerErr != nil {
			return controllerErr
		}
		current, found, err := lifecycle.ReadRuntimeTransitionStatus(context.Background(), r.lifecycleState)
		if err != nil {
			return fmt.Errorf("read Runtime transition status: %w", err)
		}
		if found && current.Sequence == sequence {
			switch current.Stage {
			case lifecycle.RuntimeTransitionDone:
				if err := ctx.Err(); err != nil {
					return err
				}
				return nil
			case lifecycle.RuntimeTransitionFailed:
				r.mu.Lock()
				unsafe := r.transitionUnsafe
				r.mu.Unlock()
				if unsafe != nil {
					return unsafe
				}
				if err := ctx.Err(); err != nil {
					return err
				}
				if code := codes.Code(current.ErrorCode); code != codes.Unknown {
					return status.Error(code, current.Error)
				}
				return errors.New(current.Error)
			}
		}
		select {
		case <-changed:
		case <-r.lifecycleDone:
			r.mu.Lock()
			failure := r.failureLocked()
			r.mu.Unlock()
			if failure != nil {
				return failure
			}
			return errors.New("Runtime lifecycle engine exited before transition completed")
		}
	}
}
