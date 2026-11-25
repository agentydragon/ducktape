// Package sessions owns browser Session creation routes. It consumes the current Sandbox identity
// and narrow Session, Environment and Thread dependencies, and emits generated AppService ProtoJSON
// responses. The caller registrar enforces browser authentication; the Dispatcher owns durable
// Session delivery, and the threads package owns Thread metadata.
package sessions

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"unicode/utf8"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sessionclient"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
)

type SessionOpener interface {
	CreateSession(context.Context, string, string, uuid.UUID, *sessionpb.SessionSpec, *string) (*dispatcherpb.Session, error)
}

type EnvironmentReader interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

type ThreadOpener interface {
	OpenThread(context.Context, uuid.UUID) error
}

type Dependencies struct {
	SandboxNamespace string
	SandboxPresets   map[string]settings.SandboxPreset
	ThreadPresets    map[string]settings.ThreadPreset
	Environments     EnvironmentReader
	SessionOpener    SessionOpener
	ThreadOpener     ThreadOpener
}

// Handler implements the Session browser operation.
type Handler struct {
	// The generated in-process gateway accepts the full aggregate service interface. This owner
	// intentionally implements only Session methods; all other RPCs stay unimplemented here and are
	// not mounted by RegisterRoutes.
	apppb.UnimplementedAppServiceServer
	sandboxNamespace string
	sandboxPresets   map[string]settings.SandboxPreset
	threadPresets    map[string]settings.ThreadPreset
	environments     EnvironmentReader
	session          SessionOpener
	thread           ThreadOpener
}

func NewHandler(dependencies Dependencies) *Handler {
	return &Handler{
		sandboxNamespace: dependencies.SandboxNamespace,
		sandboxPresets:   dependencies.SandboxPresets, threadPresets: dependencies.ThreadPresets,
		environments: dependencies.Environments, session: dependencies.SessionOpener,
		thread: dependencies.ThreadOpener,
	}
}

func validateSessionIdentity(name, id string) (uuid.UUID, error) {
	if !sandboxapi.IsValidSandboxName(name) {
		return uuid.Nil, status.Error(codes.InvalidArgument, "invalid Sandbox name")
	}
	parsed, err := uuid.Parse(id)
	if err != nil {
		return uuid.Nil, status.Error(codes.InvalidArgument, "session_id must be a UUID")
	}
	return parsed, nil
}

func (h *Handler) CreateSession(ctx context.Context, request *apppb.CreateSessionRequest) (_ *apppb.CreateSessionResponse, routeErr error) {
	ctx, span := observability.Start(ctx, "agentplane.session.create")
	defer func() { observability.End(span, routeErr) }()
	id, err := validateSessionIdentity(request.GetName(), request.GetSessionId())
	if err != nil {
		return nil, err
	}
	if request.GetSpec() == nil || request.GetSandboxUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "spec and sandbox_uid are required")
	}
	var setupScript *string
	if value := request.GetSetupScript(); value != nil {
		switch kind := value.GetKind().(type) {
		case *structpb.Value_NullValue:
		case *structpb.Value_StringValue:
			setupScript = &kind.StringValue
		default:
			return nil, status.Error(codes.InvalidArgument, "setup_script must be a string or null")
		}
	}
	sandbox, err := h.environments.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{
		Namespace: h.sandboxNamespace, Name: request.GetName(), Uid: request.GetSandboxUid(),
	}})
	if err != nil {
		return nil, sessionRPCError(err, "Environment service could not look up the Sandbox")
	}
	defaults, setupScript, err := sessionDefaults(h.sandboxPresets, h.threadPresets, sandbox, request.GetSetupScript() != nil, setupScript)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	spec, err := sessionclient.ParseSessionSpec(defaults, request.GetSpec().AsMap())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > sessionclient.MaxSetupScriptCharacters {
		return nil, status.Error(codes.InvalidArgument, "setup script exceeds maximum length")
	}
	spec.Cwd = replaceSessionID(spec.GetCwd(), id.String())
	if err := h.thread.OpenThread(ctx, id); err != nil {
		slog.ErrorContext(ctx, "create Thread metadata", "session_id", id, "error", err)
		return nil, status.Error(codes.Unavailable, "Thread metadata is unavailable")
	}
	session, err := h.session.CreateSession(ctx, request.GetName(), request.GetSandboxUid(), id, spec, setupScript)
	if err != nil {
		var unconfirmed *sessionclient.SessionNotFoundError
		if errors.As(err, &unconfirmed) {
			slog.ErrorContext(ctx, "Dispatcher session response did not confirm requested identity", "session_id", id, "error", err)
			return nil, status.Errorf(codes.Unknown, "Dispatcher result for session %s was not confirmed; reconcile this identity", id)
		}
		return nil, sessionRPCError(err, "Session Dispatcher could not confirm Thread creation; check this attempt before retrying")
	}
	return &apppb.CreateSessionResponse{Session: session}, nil
}

func sessionRPCError(err error, operation string) error {
	var timeout *sessionclient.DispatcherRequestTimeoutError
	var missing *sessionclient.SessionNotFoundError
	var service *sessionclient.ServiceError
	var refused *sessionclient.RefusedError
	switch {
	case errors.As(err, &timeout):
		return status.Error(codes.DeadlineExceeded, err.Error())
	case errors.As(err, &missing):
		return status.Error(codes.NotFound, err.Error())
	case errors.As(err, &service):
		return sessionUpstreamRPCError(service.Code, service.Details, err, operation)
	case errors.As(err, &refused):
		return status.Error(refused.Code, refused.Details)
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "Request canceled")
	case errors.Is(err, context.DeadlineExceeded):
		return sessionUpstreamRPCError(codes.DeadlineExceeded, "", err, operation)
	}
	if upstream, ok := status.FromError(err); ok {
		return sessionUpstreamRPCError(upstream.Code(), upstream.Message(), err, operation)
	}
	return sessionUpstreamRPCError(codes.Unavailable, "", err, operation)
}

func sessionUpstreamRPCError(code codes.Code, message string, cause error, operation string) error {
	switch code {
	case codes.Unknown, codes.DeadlineExceeded, codes.Unimplemented, codes.Internal, codes.Unavailable, codes.DataLoss:
		slog.Error("Session dependency failed", "operation", operation, "error", cause)
		return status.Errorf(code, "%s (%s)", operation, code)
	default:
		if upstream, ok := status.FromError(cause); ok && upstream.Code() == code {
			return upstream.Err()
		}
		return status.Error(code, message)
	}
}

func sessionDefaults(sandboxPresets map[string]settings.SandboxPreset, threadPresets map[string]settings.ThreadPreset, sandbox *environmentpb.EnvironmentDescriptor, setupWasSet bool, requestedSetup *string) (*sessionpb.SessionSpec, *string, error) {
	defaults := &sessionpb.SessionSpec{}
	var setupScript *string
	if presetName := sandbox.GetConfiguration().GetPreset(); presetName != "" {
		preset, ok := sandboxPresets[presetName]
		if !ok {
			return nil, nil, errors.New("Sandbox names preset " + quote(presetName) + ", but it is not in the app's preset catalog")
		}
		thread, ok := threadPresets[preset.ThreadPreset]
		if !ok {
			return nil, nil, errors.New("Sandbox preset " + quote(presetName) + " names an unknown ThreadPreset")
		}
		harness, ok := sessionpb.Harness_value[thread.Harness]
		if !ok {
			return nil, nil, errors.New("ThreadPreset names an unknown Harness " + quote(thread.Harness))
		}
		target := sandbox.GetTarget()
		exact := &sessionpb.ExactEnvironment{Name: target.GetName(), Uid: target.GetUid()}
		if sessionpb.Harness(harness) == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
			defaults.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: exact}}}
		} else {
			defaults.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: exact, Harness: sessionpb.Harness(harness)}}}
		}
		defaults.Model = thread.Model
		defaults.Cwd = thread.CWD
		if thread.ReasoningEffort != nil {
			defaults.ReasoningEffort = *thread.ReasoningEffort
		}
		defaults.Instructions = thread.Instructions
		script := thread.SetupScript
		setupScript = &script
	}
	if setupWasSet {
		setupScript = requestedSetup
	}
	return defaults, setupScript, nil
}

func replaceSessionID(value, sessionID string) string {
	return strings.ReplaceAll(value, "{session_id}", sessionID)
}

func quote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "\\'") + "'"
}
