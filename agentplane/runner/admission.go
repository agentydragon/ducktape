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
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
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
	if _, err := ValidateSessionID(request.SessionId); err != nil {
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
	if _, err := ResolveWorkspace(spec.Cwd, r.config.WorkspaceRoot); err != nil {
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
		s, err := r.load(r.ctx, request.SessionId)
		if err != nil {
			return nil, rpcError(err)
		}
		if !proto.Equal(s.attached().Spec, spec) {
			return nil, status.Errorf(codes.AlreadyExists, "session %s exists with a different spec", request.GetSessionId())
		}
		if request.SetupScript != nil {
			hash := sha256.Sum256([]byte(*request.SetupScript))
			var selected *string
			if *request.SetupScript != "" {
				value := hex.EncodeToString(hash[:])
				selected = &value
			}
			if !equalString(s.record.SetupScriptSHA256, selected) {
				return nil, status.Error(codes.AlreadyExists, "session exists with a different setup script")
			}
		}
		return &runnerservicepb.OpenSessionResponse{Attached: s.attached()}, nil
	}
	budget, err := r.config.ResolveModelConfig(ctx, spec.Harness, spec.Model)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	record, err := NewSessionRecord(spec, request.GetSetupScript(), budget)
	if err != nil {
		return nil, rpcError(err)
	}
	r.mu.Lock()
	if !r.lifecycle.accepting() {
		r.mu.Unlock()
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	r.admissions++
	err = r.store.Write(request.SessionId, record)
	r.mu.Unlock()
	r.changed()
	defer func() { r.mu.Lock(); r.admissions--; r.mu.Unlock(); r.changed() }()
	if err != nil {
		return nil, rpcError(err)
	}
	s, err := r.load(r.ctx, request.SessionId)
	if err != nil {
		return nil, rpcError(err)
	}
	if record.SetupScriptSHA256 != nil {
		script := request.GetSetupScript()
		s.mu.Lock()
		s.setupScript = &script
		s.mu.Unlock()
		s.wake()
	}
	r.changed()
	return &runnerservicepb.OpenSessionResponse{Attached: s.attached()}, nil
}

func equalString(a, b *string) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }

func (r *Runner) admit(id string, c *commandpb.Command) (*eventlogpb.EventEntry, error) {
	if c.CommandId == "" || c.Operation == nil {
		return nil, status.Error(codes.InvalidArgument, "command_id and operation required")
	}
	if v := c.GetInterruptTurn(); v != nil && v.TurnId == "" {
		return nil, status.Error(codes.InvalidArgument, "interrupt_turn.turn_id is required")
	}
	r.mu.Lock()
	desiredState, progress := r.lifecycle.desiredState(), r.lifecycle.status()
	if desiredState.Mode == runnerClosing && progress.Generation == desiredState.Generation && progress.Stage == stageClosed {
		r.mu.Unlock()
		return nil, status.Error(codes.Unavailable, "runner is closed")
	}
	lameduck := !r.lifecycle.accepting()
	r.admissions++
	r.idleSince = nil
	r.mu.Unlock()
	r.changed()
	defer func() { r.mu.Lock(); r.admissions--; r.mu.Unlock(); r.changed() }()
	exists, err := r.store.Exists(id)
	if err != nil {
		return nil, rpcError(err)
	}
	if !exists {
		return nil, status.Error(codes.NotFound, "no such session")
	}
	s, err := r.load(r.ctx, id)
	if err != nil {
		return nil, rpcError(err)
	}
	entry, err := s.admit(r.ctx, c, lameduck)
	return entry, rpcError(err)
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
	case errors.Is(err, ErrCommandConflict):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, ErrActiveTurnChanged):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, ErrJournalSealed):
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	return status.Error(codes.Unavailable, err.Error())
}

func (s *Session) admit(ctx context.Context, command *commandpb.Command, lameduck bool) (*eventlogpb.EventEntry, error) {
	s.admitMu.Lock()
	defer s.admitMu.Unlock()
	old, err := s.journal.GetCommand(ctx, command.CommandId)
	if err != nil {
		return nil, err
	}
	closed := s.executionClosed()
	if old == nil && (lameduck || closed) && command.GetInterruptTurn() == nil {
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	expected := ""
	if v := command.GetInterruptTurn(); v != nil {
		expected = v.TurnId
	}
	entry, _, err := s.journal.Admit(ctx, command, expected)
	if err != nil {
		return nil, err
	}
	if old != nil {
		if closed {
			return entry, nil
		}
	}
	if old != nil && old.TerminalCursor != nil {
		return entry, nil
	}
	s.mu.Lock()
	s.pending[command.CommandId] = true
	ready := s.readyLocked()
	s.mu.Unlock()
	if command.GetInterruptTurn() != nil {
		if ready {
			s.requestExecution()
		} else {
			if err := s.NoopCommand(ctx, command.CommandId, "the harness is not ready to receive this interruption", nil); err != nil {
				return nil, err
			}
		}
	} else if !lameduck && !closed {
		s.requestExecution()
	}
	s.runner.changed()
	return entry, nil
}
