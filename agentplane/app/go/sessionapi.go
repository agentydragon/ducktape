package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf8"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"google.golang.org/grpc"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

type sessionOpener interface {
	CreateSession(context.Context, string, string, uuid.UUID, *sessionpb.SessionSpec, *string) (*dispatcherpb.Session, error)
}

type sessionEnvironmentReader interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

type threadOpener interface {
	OpenThread(context.Context, uuid.UUID) error
}

// The generated unary gateway invokes the app-owned service in process, after RequireCaller
// authenticates HTTP callers and enforces cookie CSRF. Dispatcher remains a native gRPC call.
func RegisterSessionRoutes(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux(
		gatewayruntime.WithForwardResponseOption(func(ctx context.Context, w http.ResponseWriter, _ proto.Message) error {
			if method, ok := gatewayruntime.RPCMethod(ctx); ok && strings.HasSuffix(method, "/CreateSession") {
				w.WriteHeader(http.StatusCreated)
			}
			return nil
		}),
		gatewayruntime.WithErrorHandler(sessionGatewayError),
	)
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return err
	}
	server.RegisterRawRoute("POST /sandboxes/{name}/sessions", mux, false)
	return nil
}

var sessionSandboxName = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)

func validateSessionIdentity(name, id string) (uuid.UUID, error) {
	if len(name) < 1 || len(name) > 57 || !sessionSandboxName.MatchString(name) {
		return uuid.Nil, status.Error(codes.InvalidArgument, "invalid Sandbox name")
	}
	parsed, err := uuid.Parse(id)
	if err != nil {
		return uuid.Nil, status.Error(codes.InvalidArgument, "session_id must be a UUID")
	}
	return parsed, nil
}

func (s *appService) CreateSession(ctx context.Context, request *apppb.CreateSessionRequest) (_ *apppb.CreateSessionResponse, routeErr error) {
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
	sandbox, err := s.environments.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{
		Namespace: s.config.SandboxNamespace, Name: request.GetName(), Uid: request.GetSandboxUid(),
	}})
	if err != nil {
		return nil, sessionRPCError(err)
	}
	defaults, setupScript, err := sessionDefaults(s.config, sandbox, request.GetSetupScript() != nil, setupScript)
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
	if err := s.threadWriter.OpenThread(ctx, id); err != nil {
		slog.ErrorContext(ctx, "create Thread metadata", "session_id", id, "error", err)
		return nil, status.Error(codes.Unavailable, "Thread metadata is unavailable")
	}
	session, err := s.bridge.CreateSession(ctx, request.GetName(), request.GetSandboxUid(), id, spec, setupScript)
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

// Hide internal dependency failures using gRPC semantics, before the HTTP facade sees them.
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

// Retain HTTP statuses and detail for cached clients while exposing the gRPC status to new clients.
func sessionGatewayError(_ context.Context, _ *gatewayruntime.ServeMux, _ gatewayruntime.Marshaler, w http.ResponseWriter, _ *http.Request, err error) {
	st := status.Convert(err)
	httpStatus := gatewayruntime.HTTPStatusFromCode(st.Code())
	switch st.Code() {
	case codes.InvalidArgument:
		httpStatus = http.StatusUnprocessableEntity
	case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
		httpStatus = http.StatusConflict
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(httpStatus)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": int32(st.Code()), "message": st.Message(), "detail": st.Message()})
}

func sessionDefaults(config Config, sandbox *environmentpb.EnvironmentDescriptor, setupWasSet bool, requestedSetup *string) (*sessionpb.SessionSpec, *string, error) {
	defaults := &sessionpb.SessionSpec{}
	var setupScript *string
	if presetName := sandbox.GetConfiguration().GetPreset(); presetName != "" {
		preset, ok := config.SandboxPresets[presetName]
		if !ok {
			return nil, nil, errors.New("Sandbox names preset " + quote(presetName) + ", but it is not in the app's preset catalog")
		}
		thread, ok := config.ThreadPresets[preset.ThreadPreset]
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
