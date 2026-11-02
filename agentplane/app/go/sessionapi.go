package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	sessionopeningpb "github.com/agentydragon/ducktape/agentplane/app/session_opening/sessionopeningpb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

type sessionOpener interface {
	OpenSession(context.Context, string, string, uuid.UUID, *sessionpb.SessionSpec, *string) (*dispatcherpb.SessionOpening, error)
	SessionOpening(context.Context, string, uuid.UUID) (*dispatcherpb.SessionOpening, error)
}

type threadOpener interface {
	OpenThread(context.Context, uuid.UUID) error
}

type sessionOpeningService struct {
	sessionopeningpb.UnimplementedSessionOpeningServiceServer
	config    Config
	inventory appbridge.SandboxGetter
	bridge    sessionOpener
	store     threadOpener
}

// The native listener is private to this process's UID (MkdirTemp creates a 0700 directory).
// HTTP callers pass RequireCaller, including cookie CSRF, before the generated gateway dials it.
func RegisterSessionRoutes(server *HTTPServer, config Config, inventory appbridge.SandboxGetter, bridge sessionOpener, store threadOpener) error {
	socketDir, err := os.MkdirTemp("", "agentplane-session-opening-")
	if err != nil {
		return err
	}
	socketPath := filepath.Join(socketDir, "grpc.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		_ = os.RemoveAll(socketDir)
		return err
	}
	grpcServer := grpc.NewServer()
	sessionopeningpb.RegisterSessionOpeningServiceServer(grpcServer, &sessionOpeningService{config: config, inventory: inventory, bridge: bridge, store: store})
	go func() { _ = grpcServer.Serve(listener) }()
	connection, err := grpc.NewClient("unix:"+socketPath, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	if err != nil {
		grpcServer.Stop()
		_ = os.RemoveAll(socketDir)
		return err
	}
	cleanup := func() { _ = connection.Close(); grpcServer.Stop(); _ = os.RemoveAll(socketDir) }
	mux := gatewayruntime.NewServeMux(
		gatewayruntime.WithForwardResponseOption(func(ctx context.Context, w http.ResponseWriter, _ proto.Message) error {
			if method, ok := gatewayruntime.RPCMethod(ctx); ok && strings.HasSuffix(method, "/OpenSession") {
				w.WriteHeader(http.StatusCreated)
			}
			return nil
		}),
		gatewayruntime.WithErrorHandler(sessionGatewayError),
	)
	if err := sessionopeningpb.RegisterSessionOpeningServiceHandlerClient(context.Background(), mux, sessionopeningpb.NewSessionOpeningServiceClient(connection)); err != nil {
		cleanup()
		return err
	}
	server.addCleanup(cleanup)
	server.RegisterRawRoute("POST /sandboxes/{name}/sessions", mux, false)
	server.RegisterRawRoute("GET /sandboxes/{name}/sessions/{session_id}/opening", mux, false)
	return nil
}

var sessionSandboxName = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)

func validateSessionOpeningIdentity(name, id string) (uuid.UUID, error) {
	if len(name) < 1 || len(name) > 57 || !sessionSandboxName.MatchString(name) {
		return uuid.Nil, status.Error(codes.InvalidArgument, "invalid Sandbox name")
	}
	parsed, err := uuid.Parse(id)
	if err != nil {
		return uuid.Nil, status.Error(codes.InvalidArgument, "session_id must be a UUID")
	}
	return parsed, nil
}

func (s *sessionOpeningService) OpenSession(ctx context.Context, request *sessionopeningpb.OpenSessionRequest) (_ *sessionopeningpb.OpenSessionResponse, routeErr error) {
	ctx, span := observability.Start(ctx, "agentplane.session.create")
	defer func() { observability.End(span, routeErr) }()
	id, err := validateSessionOpeningIdentity(request.GetName(), request.GetSessionId())
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
	sandbox, err := s.inventory.Get(ctx, request.GetName())
	if err != nil {
		return nil, sessionRPCError(err)
	}
	if sandbox.GetUid() != request.GetSandboxUid() {
		return nil, status.Error(codes.Aborted, "Sandbox identity changed")
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
	if err := s.store.OpenThread(ctx, id); err != nil {
		slog.ErrorContext(ctx, "create Thread metadata", "session_id", id, "error", err)
		return nil, status.Error(codes.Unavailable, "Thread metadata is unavailable")
	}
	opening, err := s.bridge.OpenSession(ctx, request.GetName(), request.GetSandboxUid(), id, spec, setupScript)
	if err != nil {
		var unconfirmed *appbridge.SessionOpeningNotFoundError
		if errors.As(err, &unconfirmed) {
			slog.ErrorContext(ctx, "Dispatcher opening response did not confirm requested identity", "session_id", id, "error", err)
			return nil, status.Errorf(codes.Unknown, "Dispatcher result for session %s was not confirmed; reconcile this identity", id)
		}
		return nil, sessionRPCError(err)
	}
	return &sessionopeningpb.OpenSessionResponse{Opening: opening}, nil
}

func (s *sessionOpeningService) GetSessionOpening(ctx context.Context, request *sessionopeningpb.GetSessionOpeningRequest) (*sessionopeningpb.GetSessionOpeningResponse, error) {
	id, err := validateSessionOpeningIdentity(request.GetName(), request.GetSessionId())
	if err != nil {
		return nil, err
	}
	opening, err := s.bridge.SessionOpening(ctx, request.GetName(), id)
	if err != nil {
		return nil, sessionRPCError(err)
	}
	return &sessionopeningpb.GetSessionOpeningResponse{Opening: opening}, nil
}

func sessionRPCError(err error) error {
	var timeout *appbridge.DispatcherRequestTimeoutError
	var missing *appbridge.SessionOpeningNotFoundError
	var service *appbridge.ServiceError
	var refused *appbridge.RefusedError
	var notFound *sandboxes.SandboxNotFoundError
	var invalid *sandboxes.InvalidSandboxError
	var conflict *sandboxes.SandboxConflictError
	var running *sandboxes.SandboxRunningError
	var modeUnknown *sandboxes.SandboxModeUnknownError
	var tearingDown *sandboxes.SandboxTearingDownError
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
	case errors.As(err, &notFound), apierrors.IsNotFound(err):
		return status.Error(codes.NotFound, err.Error())
	case errors.As(err, &invalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.As(err, &conflict), errors.As(err, &running), errors.As(err, &modeUnknown), errors.As(err, &tearingDown), apierrors.IsConflict(err):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.As(err, &quiescing):
		slog.Error("Session opening dependency failed", "error", err)
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
		slog.Error("Session opening dependency failed", "error", cause)
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

func sessionDefaults(config Config, sandbox *sandboxespb.Sandbox, setupWasSet bool, requestedSetup *string) (*sessionpb.SessionSpec, *string, error) {
	defaults := &sessionpb.SessionSpec{}
	var setupScript *string
	if presetName := sandbox.GetPreset(); presetName != "" {
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
		defaults.Harness = sessionpb.Harness(harness)
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
