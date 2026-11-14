package sessions

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"unicode/utf8"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	appbridge "github.com/agentydragon/ducktape/agentplane/app/internal/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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

type RuntimeStatusReader interface {
	EnvironmentRuntimeStatus(context.Context, string, string) (*sessionpb.EnvironmentRuntimeStatus, error)
}

type Dependencies struct {
	SandboxNamespace string
	SandboxPresets   map[string]settings.SandboxPreset
	ThreadPresets    map[string]settings.ThreadPreset
	Environments     EnvironmentReader
	SessionOpener    SessionOpener
	ThreadOpener     ThreadOpener
	RuntimeStatus    RuntimeStatusReader
}

// Handler implements the Session and Environment Runtime browser operations.
type Handler struct {
	sandboxNamespace string
	sandboxPresets   map[string]settings.SandboxPreset
	threadPresets    map[string]settings.ThreadPreset
	environments     EnvironmentReader
	session          SessionOpener
	thread           ThreadOpener
	runtime          RuntimeStatusReader
}

func NewHandler(dependencies Dependencies) *Handler {
	return &Handler{
		sandboxNamespace: dependencies.SandboxNamespace,
		sandboxPresets:   dependencies.SandboxPresets, threadPresets: dependencies.ThreadPresets,
		environments: dependencies.Environments, session: dependencies.SessionOpener,
		thread: dependencies.ThreadOpener, runtime: dependencies.RuntimeStatus,
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
		return nil, sessionRPCError(err)
	}
	defaults, setupScript, err := sessionDefaults(h.sandboxPresets, h.threadPresets, sandbox, request.GetSetupScript() != nil, setupScript)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	spec, err := appbridge.ParseSessionSpec(defaults, request.GetSpec().AsMap())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > appbridge.MaxSetupScriptCharacters {
		return nil, status.Error(codes.InvalidArgument, "setup script exceeds maximum length")
	}
	spec.Cwd = replaceSessionID(spec.GetCwd(), id.String())
	if err := h.thread.OpenThread(ctx, id); err != nil {
		slog.ErrorContext(ctx, "create Thread metadata", "session_id", id, "error", err)
		return nil, status.Error(codes.Unavailable, "Thread metadata is unavailable")
	}
	session, err := h.session.CreateSession(ctx, request.GetName(), request.GetSandboxUid(), id, spec, setupScript)
	if err != nil {
		var unconfirmed *appbridge.SessionNotFoundError
		if errors.As(err, &unconfirmed) {
			slog.ErrorContext(ctx, "Dispatcher session response did not confirm requested identity", "session_id", id, "error", err)
			return nil, status.Errorf(codes.Unknown, "Dispatcher result for session %s was not confirmed; reconcile this identity", id)
		}
		return nil, sessionRPCError(err)
	}
	return &apppb.CreateSessionResponse{Session: session}, nil
}

func (h *Handler) GetEnvironmentRuntimeStatus(
	ctx context.Context, request *apppb.GetEnvironmentRuntimeStatusRequest,
) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if h.runtime == nil {
		slog.ErrorContext(ctx, "Runner status proxy is not configured")
		return nil, status.Error(codes.Unavailable, "Upstream service unavailable")
	}
	if request.GetName() == "" || request.GetSandboxUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "Sandbox name and UID are required")
	}
	runtimeStatus, err := h.runtime.EnvironmentRuntimeStatus(ctx, request.GetName(), request.GetSandboxUid())
	if err != nil {
		return nil, runnerStatusRPCError(err)
	}
	return runtimeStatus, nil
}

func sessionRPCError(err error) error {
	var timeout *appbridge.DispatcherRequestTimeoutError
	var missing *appbridge.SessionNotFoundError
	var service *appbridge.ServiceError
	var refused *appbridge.RefusedError
	var quiescing *sandboxes.SandboxQuiescingError
	switch {
	case errors.As(err, &timeout):
		return status.Error(codes.DeadlineExceeded, err.Error())
	case errors.As(err, &missing):
		return status.Error(codes.NotFound, err.Error())
	case errors.As(err, &service):
		return sessionUpstreamRPCError(service.Code, service.Details, err)
	case errors.As(err, &refused):
		return status.Error(refused.Code, refused.Details)
	case apierrors.IsNotFound(err):
		return status.Error(codes.NotFound, err.Error())
	case apierrors.IsConflict(err):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.As(err, &quiescing):
		slog.Error("Session dependency failed", "error", err)
		return status.Error(codes.Unavailable, err.Error())
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "Request canceled")
	case errors.Is(err, context.DeadlineExceeded), apierrors.IsTimeout(err), apierrors.IsServerTimeout(err):
		return sessionUpstreamRPCError(codes.DeadlineExceeded, "", err)
	case apierrors.IsTooManyRequests(err):
		return status.Error(codes.ResourceExhausted, err.Error())
	}
	if upstream, ok := status.FromError(err); ok {
		return sessionUpstreamRPCError(upstream.Code(), upstream.Message(), err)
	}
	return sessionUpstreamRPCError(codes.Unavailable, "", err)
}

func sessionUpstreamRPCError(code codes.Code, message string, cause error) error {
	switch code {
	case codes.Unknown, codes.DeadlineExceeded, codes.Unimplemented, codes.Internal, codes.Unavailable, codes.DataLoss:
		slog.Error("Session dependency failed", "error", cause)
		return status.Error(code, "Upstream service unavailable")
	default:
		if upstream, ok := status.FromError(cause); ok && upstream.Code() == code {
			return upstream.Err()
		}
		return status.Error(code, message)
	}
}

func runnerStatusRPCError(err error) error {
	var service *appbridge.ServiceError
	var refused *appbridge.RefusedError
	if errors.As(err, &service) || errors.As(err, &refused) || errors.Is(err, context.Canceled) {
		slog.Error("upstream request failed", "status", 503, "response", "Upstream service unavailable", "error", err)
		return status.Error(codes.Unavailable, "Upstream service unavailable")
	}
	return sessionRPCError(err)
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
