package centralsampler

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerpbservice "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type Sampler struct {
	samplerpb.UnimplementedSamplerServer
	Store    *Store
	Runtime  *Runtime
	Resolver *auth.Resolver
	Settings Settings
	Wakeups  *wakeups.Hub
}

func (s *Sampler) admitted(ctx context.Context) (context.Context, context.CancelFunc, auth.Principal, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.Settings.RPCTimeoutSeconds)*time.Second)
	md, _ := metadata.FromIncomingContext(ctx)
	token, err := auth.BearerToken(md.Get("authorization"))
	if err != nil {
		return ctx, cancel, auth.Principal{}, status.Error(codes.Unauthenticated, "workload bearer refused")
	}
	principal, err := s.Resolver.ResolveToken(ctx, token)
	if errors.Is(err, auth.ErrInvalidBearer) {
		err = status.Error(codes.Unauthenticated, "workload bearer refused")
	} else if err != nil {
		err = samplerError(ctx, err)
	} else if !s.Settings.AllowsAccount(principal.Account.Namespace, principal.Account.Name) {
		err = status.Error(codes.PermissionDenied, "session access refused")
	}
	return ctx, cancel, principal, err
}

func samplerError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	switch {
	case errors.Is(err, ErrForbidden):
		return status.Error(codes.PermissionDenied, "session access refused")
	case errors.Is(err, ErrConflict):
		return status.Error(codes.AlreadyExists, "identifier already has another payload")
	case errors.Is(err, ErrMissing):
		return status.Error(codes.NotFound, "session or command not found")
	case errors.Is(err, ErrPrecondition):
		return status.Error(codes.FailedPrecondition, "session cannot apply this operation")
	case errors.Is(err, ErrInvalid):
		return status.Error(codes.InvalidArgument, "invalid sampler request")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "sampler operation timed out")
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "sampler operation canceled")
	default:
		slog.Error("Sampler backend request failed", "error_type", fmt.Sprintf("%T", err))
		return status.Error(codes.Unavailable, "sampler backend unavailable")
	}
}

func (s *Sampler) OpenSession(ctx context.Context, request *samplerpb.OpenSessionRequest) (*samplerpb.OpenSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	spec := request.GetSpec()
	executor := request.GetExecutor()
	id, parseErr := uuid.Parse(request.GetSessionId())
	invalid := func() (*samplerpb.OpenSessionResponse, error) {
		return nil, status.Error(codes.InvalidArgument, "invalid sampler request")
	}
	if parseErr != nil || id.String() != request.GetSessionId() || request.GetExpectedIncarnationUid() == "" || executor == nil || executor.GetSandbox() == "" || executor.GetSandboxUid() == "" || executor.GetOwner().GetNamespace() == "" || executor.GetOwner().GetName() == "" {
		return invalid()
	}
	if s.Settings.GatewayTarget != "" {
		if _, err := s.Settings.GatewayAddress(executor.GetOwner().GetNamespace(), executor.GetSandbox()); err != nil {
			return invalid()
		}
	}
	if len(spec.GetInstructions()) > 1024*1024 || len(spec.GetCwd()) > 4096 || len(request.GetSetupScript()) > 65536 || spec.GetHarness() != runnerpb.Harness_HARNESS_CENTRAL_SAMPLER || !strings.HasPrefix(spec.GetCwd(), "/") {
		return invalid()
	}
	modelConfig, modelConfigured := s.Settings.Models[spec.GetModel()]
	if !modelConfigured || !slices.Contains(modelConfig.ReasoningEfforts, spec.GetReasoningEffort()) {
		return invalid()
	}
	session, err := s.Store.OpenSession(ctx, request, principal.ServiceAccountUID, modelConfig.API)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	if session.SetupState == runnerpb.SetupState_SETUP_STATE_RUNNING {
		s.Runtime.Wake(session.ID)
	}
	description, err := Describe(session)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	return &samplerpb.OpenSessionResponse{Attached: description.GetAttached()}, nil
}

func (s *Sampler) command(ctx context.Context, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	if command.GetCommandId() == "" || len(command.GetCommandId()) > 128 {
		return nil, status.Error(codes.InvalidArgument, "invalid sampler request")
	}
	if input := command.GetSubmitInput(); input != nil && (input.GetText() == "" || len(input.GetText()) > 1024*1024) {
		return nil, status.Error(codes.InvalidArgument, "invalid sampler request")
	}
	validate := func(session *Session) error {
		currentConfig, ok := s.Settings.Models[session.Model]
		if !ok {
			return ErrPrecondition
		}
		if model := command.GetChangeModel(); model != nil {
			targetConfig, targetConfigured := s.Settings.Models[model.GetModel()]
			if !targetConfigured || !slices.Contains(targetConfig.ReasoningEfforts, session.Effort) {
				return ErrInvalid
			}
			if !currentConfig.Compatible(targetConfig) {
				return ErrPrecondition
			}
		}
		if effort := command.GetChangeReasoningEffort(); effort != nil && !slices.Contains(currentConfig.ReasoningEfforts, effort.GetEffort()) {
			return ErrInvalid
		}
		if effort := command.GetChangeReasoningEffort(); effort != nil && currentConfig.API == APIStyleAnthropicMessages && !currentConfig.AnthropicMessages.PerMessageEffort {
			return ErrPrecondition
		}
		if command.GetCompact() != nil && (currentConfig.Responses.Dialect == ResponsesDialectStandard || (currentConfig.API == APIStyleAnthropicMessages && currentConfig.AnthropicMessages.Compaction == nil)) {
			return ErrPrecondition
		}
		return nil
	}
	entry, err := s.Store.Admit(ctx, sessionID, command, principal.ServiceAccountUID, validate)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	s.Runtime.Wake(sessionID)
	return entry, nil
}

func (s *Sampler) SubmitInput(ctx context.Context, r *runnerpbservice.SubmitInputRequest) (*runnerpbservice.SubmitInputResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: r.GetText()}}})
	return &runnerpbservice.SubmitInputResponse{Admission: entry}, err
}

func (s *Sampler) ChangeModel(ctx context.Context, r *runnerpbservice.ChangeModelRequest) (*runnerpbservice.ChangeModelResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: r.GetModel()}}})
	return &runnerpbservice.ChangeModelResponse{Admission: entry}, err
}

func (s *Sampler) ChangeReasoningEffort(ctx context.Context, r *runnerpbservice.ChangeReasoningEffortRequest) (*runnerpbservice.ChangeReasoningEffortResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: r.GetEffort()}}})
	return &runnerpbservice.ChangeReasoningEffortResponse{Admission: entry}, err
}

func (s *Sampler) InterruptTurn(ctx context.Context, r *runnerpbservice.InterruptTurnRequest) (*runnerpbservice.InterruptTurnResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: r.GetTurnId()}}})
	return &runnerpbservice.InterruptTurnResponse{Admission: entry}, err
}

func (s *Sampler) Compact(ctx context.Context, r *samplerpb.CompactRequest) (*samplerpb.CompactResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}}})
	return &samplerpb.CompactResponse{Admission: entry}, err
}

func (s *Sampler) RecoverSession(ctx context.Context, r *samplerpb.RecoverSessionRequest) (*samplerpb.GetSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	session, err := s.Store.RecoverSession(ctx, r.GetSessionId(), principal.ServiceAccountUID, r.GetExpectedCursor(), r.GetOperatorNote(), r.GetReplacementIncarnationUid())
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	description, err := Describe(session)
	return description, samplerError(ctx, err)
}

func (s *Sampler) GetSession(ctx context.Context, r *samplerpb.GetSessionRequest) (*samplerpb.GetSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	session, err := s.Store.GetSession(ctx, r.GetSessionId(), principal.ServiceAccountUID)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	description, err := Describe(session)
	return description, samplerError(ctx, err)
}

func (s *Sampler) ListSessions(ctx context.Context, r *samplerpb.ListSessionsRequest) (*samplerpb.ListSessionsResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	sessions, err := s.Store.ListSessions(ctx, principal.ServiceAccountUID)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	result := &samplerpb.ListSessionsResponse{}
	for _, session := range sessions {
		description, err := Describe(session)
		if err != nil {
			return nil, samplerError(ctx, err)
		}
		result.Sessions = append(result.Sessions, description)
	}
	return result, nil
}

func (s *Sampler) GetCommand(ctx context.Context, r *samplerpb.GetCommandRequest) (*samplerpb.GetCommandResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	if _, err := s.Store.GetSession(ctx, r.GetSessionId(), principal.ServiceAccountUID); err != nil {
		return nil, samplerError(ctx, err)
	}
	result, err := s.Store.GetCommand(ctx, r.GetSessionId(), r.GetCommandId())
	return result, samplerError(ctx, err)
}

func (s *Sampler) FollowEvents(r *samplerpb.FollowEventsRequest, stream grpc.ServerStreamingServer[samplerpb.FollowEventsResponse]) error {
	changed, unsubscribe, err := s.Wakeups.Subscribe(OutboxChannel)
	if err != nil {
		return samplerError(stream.Context(), err)
	}
	defer unsubscribe()
	cursor := r.GetAfterCursor()
	for {
		ctx, cancel, principal, err := s.admitted(stream.Context())
		if err != nil {
			cancel()
			return err
		}
		if _, err = s.Store.GetSession(ctx, r.GetSessionId(), principal.ServiceAccountUID); err != nil {
			mapped := samplerError(ctx, err)
			cancel()
			return mapped
		}
		entries, err := s.Store.Events(ctx, r.GetSessionId(), cursor, 256)
		mapped := samplerError(ctx, err)
		cancel()
		if err != nil {
			return mapped
		}
		for _, entry := range entries {
			if err := stream.Send(&samplerpb.FollowEventsResponse{Entry: entry}); err != nil {
				return err
			}
			cursor = entry.GetCursor()
		}
		if len(entries) > 0 {
			continue
		}
		if !r.GetFollow() {
			return nil
		}
		// Recheck authentication after the cached TokenReview expires, even without journal traffic.
		timer := time.NewTimer(auth.DefaultVerdictTTL)
		select {
		case <-stream.Context().Done():
			timer.Stop()
			return status.FromContextError(stream.Context().Err()).Err()
		case <-changed:
			timer.Stop()
		case <-timer.C:
		}
	}
}
