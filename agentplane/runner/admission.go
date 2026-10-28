package runner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func (r *Runner) OpenSession(ctx context.Context, request *runnerservicepb.OpenSessionRequest) (response *runnerservicepb.OpenSessionResponse, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Runner OpenSession",
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
	if !r.lifecycle.accepting() {
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	spec := proto.Clone(request.Spec).(*runnerpb.SessionSpec)
	spec.Instructions = r.config.Launch.Instructions(request.SessionId, spec.Instructions)
	if spec.Harness != runnerpb.Harness_HARNESS_CLAUDE && spec.Harness != runnerpb.Harness_HARNESS_CODEX {
		return nil, status.Error(codes.InvalidArgument, "spec.harness must be CLAUDE or CODEX")
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
		s, err := r.sessions.Load(r.ctx, request.SessionId)
		if err != nil {
			return nil, rpcError(err)
		}
		if !proto.Equal(s.Attached().Spec, spec) {
			return nil, status.Errorf(codes.AlreadyExists, "session %s exists with a different spec", request.GetSessionId())
		}
		if request.SetupScript != nil {
			hash := sha256.Sum256([]byte(*request.SetupScript))
			var selected *string
			if *request.SetupScript != "" {
				value := hex.EncodeToString(hash[:])
				selected = &value
			}
			if !equalString(s.SetupScriptSHA256(), selected) {
				return nil, status.Error(codes.AlreadyExists, "session exists with a different setup script")
			}
		}
		return &runnerservicepb.OpenSessionResponse{Attached: s.Attached()}, nil
	}
	budget, err := r.config.ResolveModelConfig(ctx, spec.Harness, spec.Model)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	record, err := retained.NewRecord(spec, request.GetSetupScript(), budget)
	if err != nil {
		return nil, rpcError(err)
	}
	admitted := false
	s, err := r.sessions.ReserveFresh(r.ctx, request.SessionId, func() error {
		r.mu.Lock()
		if !r.lifecycle.accepting() {
			r.mu.Unlock()
			return status.Error(codes.Aborted, "this Sandbox is quiesced")
		}
		if err := r.lifecycle.changeAdmissionsLocked(1); err != nil {
			r.mu.Unlock()
			return err
		}
		r.idleSince = nil
		admitted = true
		writeErr := r.store.Write(request.SessionId, record)
		r.mu.Unlock()
		r.changed()
		return writeErr
	})
	if admitted {
		defer r.finishAdmission(&resultErr)
	}
	if err != nil {
		return nil, rpcError(err)
	}
	if record.SetupScriptSHA256 != nil {
		script := request.GetSetupScript()
		s.SetSetupScript(script)
	}
	r.changed()
	return &runnerservicepb.OpenSessionResponse{Attached: s.Attached()}, nil
}

func equalString(a, b *string) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }

func (r *Runner) admit(id string, c *commandpb.Command) (entry *eventlogpb.EventEntry, resultErr error) {
	if c.CommandId == "" || c.Operation == nil {
		return nil, status.Error(codes.InvalidArgument, "command_id and operation required")
	}
	if v := c.GetInterruptTurn(); v != nil && v.TurnId == "" {
		return nil, status.Error(codes.InvalidArgument, "interrupt_turn.turn_id is required")
	}
	r.mu.Lock()
	desiredState, progress := r.lifecycle.desiredState(), r.lifecycle.status()
	if desiredState.Mode == runnerlifecycle.Closing && progress.Generation == desiredState.Generation && progress.Stage == runnerlifecycle.Closed {
		r.mu.Unlock()
		return nil, status.Error(codes.Unavailable, "runner is closed")
	}
	lameduck := !r.lifecycle.accepting()
	if err := r.lifecycle.changeAdmissionsLocked(1); err != nil {
		r.mu.Unlock()
		return nil, rpcError(err)
	}
	r.idleSince = nil
	r.mu.Unlock()
	r.changed()
	defer r.finishAdmission(&resultErr)
	exists, err := r.store.Exists(id)
	if err != nil {
		return nil, rpcError(err)
	}
	if !exists {
		return nil, status.Error(codes.NotFound, "no such session")
	}
	s, err := r.sessions.Load(r.ctx, id)
	if err != nil {
		return nil, rpcError(err)
	}
	entry, err = s.Admit(r.ctx, c, lameduck)
	return entry, rpcError(err)
}

func (r *Runner) finishAdmission(resultErr *error) {
	r.mu.Lock()
	err := r.lifecycle.changeAdmissionsLocked(-1)
	r.mu.Unlock()
	r.changed()
	if err != nil {
		r.fail(err)
		*resultErr = status.Error(codes.Unavailable, errors.Join(*resultErr, err).Error())
	}
}

func (r *Runner) SubmitInput(_ context.Context, q *runnerservicepb.SubmitInputRequest) (*runnerservicepb.SubmitInputResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: q.Text}}})
	return &runnerservicepb.SubmitInputResponse{Admission: entry}, err
}

func (r *Runner) ChangeModel(_ context.Context, q *runnerservicepb.ChangeModelRequest) (*runnerservicepb.ChangeModelResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: q.Model}}})
	return &runnerservicepb.ChangeModelResponse{Admission: entry}, err
}

func (r *Runner) ChangeReasoningEffort(_ context.Context, q *runnerservicepb.ChangeReasoningEffortRequest) (*runnerservicepb.ChangeReasoningEffortResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: q.Effort}}})
	return &runnerservicepb.ChangeReasoningEffortResponse{Admission: entry}, err
}

func (r *Runner) InterruptTurn(_ context.Context, q *runnerservicepb.InterruptTurnRequest) (*runnerservicepb.InterruptTurnResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: q.TurnId}}})
	return &runnerservicepb.InterruptTurnResponse{Admission: entry}, err
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
