package runtime

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func (r *Runtime) CreateSession(ctx context.Context, request *environmentruntimepb.CreateSessionRequest) (response *environmentruntimepb.CreateSessionResponse, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Environment Runtime CreateSession",
		trace.WithAttributes(
			attribute.String("session.id", request.GetSessionId()),
			attribute.String("sandbox.name", r.config.Launch.Sandbox.Name),
		),
	)
	defer func() { observability.End(span, resultErr) }()
	if request.Spec == nil {
		return nil, status.Error(codes.InvalidArgument, "spec is required")
	}
	if _, err := retained.ValidateSessionID(request.SessionId); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if err := validateExecutionPrincipal(request.GetExecutionPrincipal()); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if err := validateExecutionIdentity(request.GetExecutionIdentity()); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	expected, err := r.authorizeSession(ctx, true)
	if err != nil {
		return nil, err
	}
	if r.isClosing() {
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	spec := proto.Clone(request.Spec).(*sessionpb.SessionSpec)
	spec.Instructions = r.config.Launch.Instructions(request.SessionId, spec.Instructions)
	placement := spec.GetPlacement().GetEnvironmentHosted()
	if placement == nil || (placement.GetHarness() != sessionpb.Harness_HARNESS_CLAUDE && placement.GetHarness() != sessionpb.Harness_HARNESS_CODEX) ||
		placement.GetEnvironment().GetName() != r.config.Launch.Sandbox.Name || placement.GetEnvironment().GetUid() != r.config.Launch.Sandbox.UID {
		return nil, status.Error(codes.InvalidArgument, "spec.environment_hosted must select this exact environment and a native harness")
	}
	if !filepath.IsAbs(spec.Cwd) || spec.Model == "" {
		return nil, status.Error(codes.InvalidArgument, "spec.cwd must be absolute and spec.model is required")
	}
	if _, err := workspace.Resolve(spec.Cwd, r.config.WorkspaceRoot); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if len(request.GetSetupScript()) > 65536 {
		return nil, status.Error(codes.InvalidArgument, "setup_script exceeds 65536 UTF-8 bytes")
	}
	// Serialize reservation identity, but never hold the quiesce barrier across the
	// HTTP model lookup: quiesce can reject a late creation before its durable write.
	r.createMu.Lock()
	defer r.createMu.Unlock()
	exists, err := r.store.Exists(request.SessionId)
	if err != nil {
		return nil, rpcError(err)
	}
	if exists {
		if err := r.withSessionAdmission(ctx, true, &expected, func() error {
			if err := r.changeAdmissionsLocked(1); err != nil {
				return err
			}
			r.idleSince = nil
			return nil
		}); err != nil {
			return nil, rpcError(err)
		}
		defer r.finishAdmission(&resultErr)
		record, err := r.store.Read(request.SessionId)
		if err != nil {
			return nil, rpcError(err)
		}
		if request.SetupScript != nil && !retained.MatchesSetupScript(record, request.GetSetupScript()) {
			return nil, status.Error(codes.AlreadyExists, "session exists with a different setup script")
		}
		if !compatibleExecutionPrincipal(record.ExecutionPrincipal, request.GetExecutionPrincipal()) {
			return nil, status.Error(codes.AlreadyExists, "session exists with a different execution principal")
		}
		if !compatibleExecutionIdentity(record.ExecutionIdentity, request.GetExecutionIdentity()) {
			return nil, status.Error(codes.AlreadyExists, "session exists with a different execution identity")
		}
		if err := r.sessions.RetryFresh(r.lifecycleContext, request.SessionId); err != nil {
			return nil, rpcError(err)
		}
		s, err := r.sessions.Wait(r.lifecycleContext, request.SessionId)
		if err != nil {
			return nil, rpcError(err)
		}
		attached := s.Attached()
		if !proto.Equal(attached.Spec, spec) {
			return nil, status.Errorf(codes.AlreadyExists, "session %s exists with a different spec", request.GetSessionId())
		}
		return &environmentruntimepb.CreateSessionResponse{Attached: attached}, nil
	}
	budget, err := r.config.ResolveModelConfig(ctx, placement.GetHarness(), spec.Model)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	record, err := retained.NewRecord(spec, request.GetSetupScript(), budget)
	if err != nil {
		return nil, rpcError(err)
	}
	if request.GetExecutionPrincipal() != nil {
		record.ExecutionPrincipal = proto.Clone(request.GetExecutionPrincipal()).(*identitypb.ServiceAccountIdentity)
	}
	if request.GetExecutionIdentity() != nil {
		record.ExecutionIdentity = proto.Clone(request.GetExecutionIdentity()).(*identitypb.Identity)
	}
	admitted := false
	s, err := r.sessions.ReserveFresh(r.lifecycleContext, request.SessionId, func() error {
		return r.withSessionAdmission(ctx, true, &expected, func() error {
			if err := r.changeAdmissionsLocked(1); err != nil {
				return err
			}
			r.idleSince = nil
			admitted = true
			if err := r.store.Write(request.SessionId, record); err != nil {
				// COSI can persist the record before reporting failure, leaving its
				// in-memory view stale. Revoke admission while holding the same lock
				// as the write; only a restart can reload the durable outcome.
				r.failLocked(fmt.Errorf("reserve Session %s: %w", request.SessionId, err))
				return err
			}
			return nil
		})
	})
	if admitted {
		defer r.finishAdmission(&resultErr)
	}
	if err != nil {
		return nil, rpcError(err)
	}
	r.changed()
	return &environmentruntimepb.CreateSessionResponse{Attached: s.Attached()}, nil
}

func validateExecutionPrincipal(principal *identitypb.ServiceAccountIdentity) error {
	if principal == nil {
		return nil
	}
	if principal.GetAccount().GetNamespace() == "" || principal.GetAccount().GetName() == "" || principal.GetUid() == "" {
		return errors.New("execution_principal must include an account namespace, account name, and UID")
	}
	return nil
}

// A retry may omit a principal after it was durably recorded, but it may not replace one or assign
// an identity to an older unassociated record.
func compatibleExecutionPrincipal(retained, incoming *identitypb.ServiceAccountIdentity) bool {
	if retained == nil {
		return incoming == nil
	}
	return incoming == nil || proto.Equal(retained, incoming)
}

func validateExecutionIdentity(identity *identitypb.Identity) error {
	if identity != nil && (identity.GetName() == "" || identity.GetUid() == "") {
		return errors.New("execution_identity must include a name and UID")
	}
	return nil
}

// compatibleExecutionIdentity is compatibleExecutionPrincipal for the Identity the Session executes as.
func compatibleExecutionIdentity(retained, incoming *identitypb.Identity) bool {
	if retained == nil {
		return incoming == nil
	}
	return incoming == nil || proto.Equal(retained, incoming)
}

func (r *Runtime) admit(ctx context.Context, id string, c *commandpb.Command) (entry *eventlogpb.EventEntry, resultErr error) {
	if c.CommandId == "" || c.Operation == nil {
		return nil, status.Error(codes.InvalidArgument, "command_id and operation required")
	}
	if v := c.GetInterruptTurn(); v != nil && v.TurnId == "" {
		return nil, status.Error(codes.InvalidArgument, "interrupt_turn.turn_id is required")
	}
	var lameduck bool
	if err := r.withSessionAdmission(ctx, false, nil, func() error {
		lameduck = !r.acceptingLocked()
		if err := r.changeAdmissionsLocked(1); err != nil {
			return err
		}
		r.idleSince = nil
		return nil
	}); err != nil {
		return nil, rpcError(err)
	}
	defer r.finishAdmission(&resultErr)
	exists, err := r.store.Exists(id)
	if err != nil {
		return nil, rpcError(err)
	}
	if !exists {
		return nil, status.Error(codes.NotFound, "no such session")
	}
	s, err := r.sessions.Wait(r.lifecycleContext, id)
	if err != nil {
		return nil, rpcError(err)
	}
	entry, err = s.Admit(r.lifecycleContext, c, lameduck)
	return entry, rpcError(err)
}

func (r *Runtime) finishAdmission(resultErr *error) {
	r.mu.Lock()
	err := r.changeAdmissionsLocked(-1)
	r.mu.Unlock()
	r.changed()
	if err != nil {
		r.fail(err)
		*resultErr = status.Error(codes.Unavailable, errors.Join(*resultErr, err).Error())
	}
}

func (r *Runtime) SubmitInput(ctx context.Context, q *environmentruntimepb.SubmitInputRequest) (*environmentruntimepb.SubmitInputResponse, error) {
	entry, err := r.admit(ctx, q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: q.Text}}})
	return &environmentruntimepb.SubmitInputResponse{Admission: entry}, err
}

func (r *Runtime) ChangeModel(ctx context.Context, q *environmentruntimepb.ChangeModelRequest) (*environmentruntimepb.ChangeModelResponse, error) {
	entry, err := r.admit(ctx, q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: q.Model}}})
	return &environmentruntimepb.ChangeModelResponse{Admission: entry}, err
}

func (r *Runtime) ChangeReasoningEffort(ctx context.Context, q *environmentruntimepb.ChangeReasoningEffortRequest) (*environmentruntimepb.ChangeReasoningEffortResponse, error) {
	entry, err := r.admit(ctx, q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: q.Effort}}})
	return &environmentruntimepb.ChangeReasoningEffortResponse{Admission: entry}, err
}

func (r *Runtime) InterruptTurn(ctx context.Context, q *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	entry, err := r.admit(ctx, q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: q.TurnId}}})
	return &environmentruntimepb.InterruptTurnResponse{Admission: entry}, err
}

func rpcError(err error) error {
	if err == nil {
		return nil
	}
	if _, ok := status.FromError(err); ok {
		return err
	}
	switch {
	case errors.Is(err, journal.ErrCommandConflict):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, journal.ErrActiveTurnChanged):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, journal.ErrSealed):
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	return status.Error(codes.Unavailable, err.Error())
}

func (r *Runtime) acceptingLocked() bool {
	return r.admissionOpen && !r.suspendPrepared && !r.closing && !r.failed && r.startupComplete && r.desired.Mode == lifecycle.Running
}

func (r *Runtime) authorizeSession(ctx context.Context, running bool) (lifecycle.EnvironmentDesired, error) {
	var expected lifecycle.EnvironmentDesired
	err := r.withSessionAdmission(ctx, running, nil, func() error { expected = r.desired; return nil })
	return expected, err
}

func (r *Runtime) withSessionAdmission(ctx context.Context, running bool, expected *lifecycle.EnvironmentDesired, admit func() error) error {
	return r.control.Admit(ctx, func(epoch *runtimecontrol.Epoch) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if err := r.authorizeSessionEpochLocked(epoch, running); err != nil {
			return err
		}
		if expected != nil && r.desired != *expected {
			return status.Error(codes.Aborted, "environment lifecycle changed before reservation")
		}
		err := admit()
		r.changedLocked()
		return err
	})
}

func (r *Runtime) authorizeSessionEpochLocked(epoch *runtimecontrol.Epoch, running bool) error {
	if r.closing || r.failed || !r.startupComplete {
		return status.Error(codes.Unavailable, "environment Runtime is unavailable")
	}
	if r.suspendPrepared {
		return status.Error(codes.Unavailable, "environment Runtime is preparing to suspend")
	}
	if running && !r.acceptingLocked() {
		return status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	if r.desired.Mode != lifecycle.Running && r.desired.Mode != lifecycle.Quiescing {
		return status.Error(codes.Unavailable, "environment is not serving Session calls")
	}
	if epoch.IncarnationUID() != r.desired.IncarnationUID {
		return status.Error(codes.Aborted, "control epoch does not match the active incarnation")
	}
	return nil
}

func (r *Runtime) authorizeSessionRead(ctx context.Context) error {
	_, err := r.authorizeSession(ctx, false)
	return err
}
