package rpc

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type Sampler struct {
	Store    *sessionstore.Store
	Runtime  *residentruntime.Runtime
	Settings samplersettings.Settings
	Wakeups  *wakeups.Hub
}

// admitted returns the exact service SPIFFE ID used as the Session owner key.
func (s *Sampler) admitted(ctx context.Context) (context.Context, context.CancelFunc, string, error) {
	return admitted(ctx, s.Settings.RPCTimeoutSeconds, s.Settings.ServiceCallers)
}

func admitted(ctx context.Context, timeoutSeconds int, serviceCallers []string) (context.Context, context.CancelFunc, string, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSeconds)*time.Second)
	rule, err := auth.Services(serviceCallers)
	if err != nil {
		return ctx, cancel, "", samplerError(ctx, err)
	}
	admitted, err := rule.AdmitContext(ctx, 0)
	if err != nil {
		if status.Code(err) == codes.Unknown {
			err = samplerError(ctx, err)
		}
		return ctx, cancel, "", err
	}
	proof, err := auth.Admitted(admitted)
	if err != nil {
		return ctx, cancel, "", err
	}
	switch caller := proof.(type) {
	case auth.ServiceAdmission:
		return admitted, cancel, caller.Caller.SPIFFEID, nil
	default:
		return admitted, cancel, "", status.Error(codes.PermissionDenied, "session access refused")
	}
}

func samplerError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	switch {
	case errors.Is(err, sessionstore.ErrForbidden):
		return status.Error(codes.PermissionDenied, "session access refused")
	case errors.Is(err, sessionstore.ErrConflict):
		return status.Error(codes.AlreadyExists, "identifier already has another payload")
	case errors.Is(err, protocol.ErrMissing):
		return status.Error(codes.NotFound, "session or command not found")
	case errors.Is(err, protocol.ErrPrecondition):
		return status.Error(codes.FailedPrecondition, "session cannot apply this operation")
	case errors.Is(err, protocol.ErrInvalid):
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

func (s *Sampler) CreateSession(ctx context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	spec := request.GetSpec()
	id, parseErr := uuid.Parse(request.GetSessionId())
	if parseErr != nil || id.String() != request.GetSessionId() ||
		len(spec.GetInstructions()) > 1024*1024 || len(spec.GetCwd()) > 4096 || len(request.GetSetupScript()) > 65536 ||
		!strings.HasPrefix(spec.GetCwd(), "/") {
		return nil, status.Error(codes.InvalidArgument, "invalid sampler request")
	}
	configuration, err := s.sessionConfiguration(request)
	if err != nil {
		return nil, err
	}
	modelConfig, modelConfigured := s.Settings.Models[spec.GetModel()]
	if !modelConfigured {
		return nil, status.Error(codes.InvalidArgument, "model is not configured for the central sampler")
	}
	if !slices.Contains(modelConfig.ReasoningEfforts, spec.GetReasoningEffort()) {
		return nil, status.Error(codes.InvalidArgument, "reasoning effort is not supported by the selected model")
	}
	session, err := s.Store.CreateSession(ctx, configuration, principal, modelConfig.API, s.Settings.PlatformInstructions)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	if session.SetupState == sessionpb.SetupState_SETUP_STATE_RUNNING {
		s.Runtime.Wake(session.ID)
	}
	description, err := sessionstore.Describe(session)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	return &harnesshostpb.CreateSessionResponse{Attached: description.GetAttached()}, nil
}

// sessionConfiguration separates caller compatibility fields from the retained executor binding.
// The stored wire shape stays readable by the supported rollback image.
func (s *Sampler) sessionConfiguration(request *harnesshostpb.CreateSessionRequest) (*sessionconfig.Configuration, error) {
	environment := request.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
	if environment.GetName() == "" || environment.GetUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "central_sampler_v1.executor_environment name and UID are required")
	}
	namespace := s.Settings.EnvironmentNamespace
	executor := &identitypb.SandboxDestination{
		Target: &identitypb.EnvironmentTarget{Namespace: namespace, Name: environment.GetName(), Uid: environment.GetUid()},
		Owner:  &identitypb.ServiceAccount{Namespace: namespace, Name: environment.GetName()},
	}
	if s.Settings.GatewayTarget != "" {
		if _, err := s.Settings.GatewayAddress(namespace, environment.GetName()); err != nil {
			return nil, status.Error(codes.InvalidArgument, "invalid executor destination")
		}
	}
	configuration := &sessionconfig.Configuration{
		SessionId: request.GetSessionId(), Spec: proto.Clone(request.GetSpec()).(*sessionpb.SessionSpec),
		Executor: executor, SetupScript: request.SetupScript, ExecutionIdentity: request.GetExecutionIdentity(),
	}
	// Only a successful first Executor OpenSession establishes a new Session's incarnation fence.
	return configuration, nil
}

func (s *Sampler) command(ctx context.Context, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	return s.commandUsing(ctx, sessionID, command, s.admitted)
}

func (s *Sampler) commandUsing(
	ctx context.Context,
	sessionID string,
	command *commandpb.Command,
	admit func(context.Context) (context.Context, context.CancelFunc, string, error),
) (*eventlogpb.EventEntry, error) {
	ctx, cancel, principal, err := admit(ctx)
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
	validate := func(session *sessionstore.Session) error {
		currentConfig, ok := s.Settings.Models[session.Model]
		if !ok {
			return protocol.ErrPrecondition
		}
		if model := command.GetChangeModel(); model != nil {
			targetConfig, targetConfigured := s.Settings.Models[model.GetModel()]
			if !targetConfigured || !slices.Contains(targetConfig.ReasoningEfforts, session.Effort) {
				return protocol.ErrInvalid
			}
			if !currentConfig.Compatible(targetConfig) {
				return protocol.ErrPrecondition
			}
		}
		if effort := command.GetChangeReasoningEffort(); effort != nil && !slices.Contains(currentConfig.ReasoningEfforts, effort.GetEffort()) {
			return protocol.ErrInvalid
		}
		if effort := command.GetChangeReasoningEffort(); effort != nil && currentConfig.API == protocol.APIStyleAnthropicMessages && !currentConfig.AnthropicMessages.PerMessageEffort {
			return protocol.ErrPrecondition
		}
		if command.GetCompact() != nil && ((currentConfig.API == protocol.APIStyleChatCompletions && currentConfig.ChatCompaction == nil) || currentConfig.Responses.Dialect == responses.DialectStandard || (currentConfig.API == protocol.APIStyleAnthropicMessages && currentConfig.AnthropicMessages.Compaction == nil)) {
			return protocol.ErrPrecondition
		}
		return nil
	}
	entry, err := s.Store.Admit(ctx, sessionID, command, principal, validate)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	s.Runtime.Wake(sessionID)
	return entry, nil
}

func (s *Sampler) SubmitInput(ctx context.Context, r *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: r.GetText()}}})
	return &harnesshostpb.SubmitInputResponse{Admission: entry}, err
}

func (s *Sampler) ChangeModel(ctx context.Context, r *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: r.GetModel()}}})
	return &harnesshostpb.ChangeModelResponse{Admission: entry}, err
}

func (s *Sampler) ChangeReasoningEffort(ctx context.Context, r *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: r.GetEffort()}}})
	return &harnesshostpb.ChangeReasoningEffortResponse{Admission: entry}, err
}

func (s *Sampler) InterruptTurn(ctx context.Context, r *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	entry, err := s.command(ctx, r.GetSessionId(), &commandpb.Command{CommandId: r.GetCommandId(), Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: r.GetTurnId()}}})
	return &harnesshostpb.InterruptTurnResponse{Admission: entry}, err
}
