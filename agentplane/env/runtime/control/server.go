package control

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	SandboxMetadata     = "agentplane-sandbox-uid"
	IncarnationMetadata = "agentplane-incarnation-uid"
	BootMetadata        = "agentplane-environment-runtime-boot-id"
	EpochMetadata       = "agentplane-connection-epoch"

	forwardHeartbeatInterval = 10 * time.Second
	forwardWriteTimeout      = 10 * time.Second
)

type Config struct {
	Interceptor   grpc.UnaryServerInterceptor
	SandboxUID    string
	BootID        string
	ListenAddress string
	Security      *environmentruntimetransport.OpenedSecurity

	IsClosing        func() bool
	AdoptIncarnation func(context.Context, string) error
	Facts            func() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error)
	Changes          func() <-chan struct{}
}

type watch struct {
	ctx            context.Context
	cancel         context.CancelFunc
	sandboxUID     string
	incarnationUID string
	epoch          uint64
	ready          bool
}

// Epoch is an opaque authorization result. Epoch zero binds admission to the adopted
// incarnation; nonzero epochs retain the legacy state-watch authorization.
type Epoch struct {
	server *Server
	watch  *watch
}

func (e *Epoch) Context() context.Context { return e.watch.ctx }

func (e *Epoch) Number() uint64 { return e.watch.epoch }

func (e *Epoch) SandboxUID() string { return e.watch.sandboxUID }

func (e *Epoch) IncarnationUID() string { return e.watch.incarnationUID }

type Server struct {
	environmentruntimepb.EnvironmentRuntimeServer

	config Config

	adoptionMu            sync.Mutex
	mu                    sync.RWMutex
	boundIncarnationUID   string
	identity              *watch
	watch                 *watch
	highestEpoch          uint64
	highestSandboxUID     string
	highestIncarnationUID string
}

func New(config Config, backend environmentruntimepb.EnvironmentRuntimeServer) *Server {
	return &Server{config: config, EnvironmentRuntimeServer: backend}
}

// Listen opens the configured authenticated control listener and registers the
// Runner, executor stream, and standard gRPC health services.
func (s *Server) Listen(executor executorpb.ExecutorServer) (net.Listener, *grpc.Server, error) {
	transportCredentials, err := s.config.Security.ServerCredentials(environmentruntimetransport.GatewayURI, environmentruntimetransport.RuntimeProxyURI, environmentruntimetransport.SandboxControllerURI)
	if err != nil {
		return nil, nil, err
	}
	listener, err := net.Listen("tcp", s.config.ListenAddress)
	if err != nil {
		return nil, nil, err
	}
	server := grpc.NewServer(
		grpc.Creds(transportCredentials),
		observability.GRPCServerOption(),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			if strings.HasPrefix(info.FullMethod, "/grpc.health.v1.Health/") {
				return handler(ctx, request)
			}
			if _, err := s.Authorize(ctx); err != nil {
				return nil, err
			}
			if s.config.Interceptor != nil {
				return s.config.Interceptor(ctx, request, info, handler)
			}
			return handler(ctx, request)
		}),
	)
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(environmentruntimepb.EnvironmentRuntime_ServiceDesc.ServiceName, grpc_health_v1.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, grpc_health_v1.HealthCheckResponse_SERVING)
	grpc_health_v1.RegisterHealthServer(server, healthServer)
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, s)
	executorpb.RegisterExecutorServer(server, executor)
	return listener, server, nil
}

func (s *Server) directWatchContext(req *environmentruntimepb.WatchStateRequest, parent context.Context) (*watch, error) {
	if req == nil || req.SandboxUid == "" || req.IncarnationUid == "" {
		return nil, status.Error(codes.InvalidArgument, "sandbox UID and incarnation UID are required")
	}
	if req.SandboxUid != s.config.SandboxUID {
		return nil, status.Error(codes.PermissionDenied, "watch request names another Sandbox")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.identity == nil || !s.identity.ready {
		return nil, status.Error(codes.Unavailable, "Sandbox Controller has not bound the Runtime incarnation")
	}
	if req.IncarnationUid != s.boundIncarnationUID {
		return nil, status.Error(codes.FailedPrecondition, "Gateway watch cannot replace the Runtime incarnation")
	}
	if req.ConnectionEpoch == 0 {
		ctx, cancel := context.WithCancel(parent)
		return &watch{ctx: ctx, cancel: cancel, sandboxUID: req.SandboxUid, incarnationUID: req.IncarnationUid}, nil
	}
	if req.ConnectionEpoch < s.highestEpoch {
		return nil, status.Error(codes.Aborted, "watch request has a stale connection epoch")
	}
	if req.ConnectionEpoch == s.highestEpoch && s.highestEpoch != 0 &&
		(req.SandboxUid != s.highestSandboxUID || req.IncarnationUid != s.highestIncarnationUID) {
		return nil, status.Error(codes.Aborted, "watch request changed identity without advancing the connection epoch")
	}
	if current := s.watch; current != nil {
		if current.epoch == req.ConnectionEpoch {
			return nil, status.Error(codes.AlreadyExists, "connection epoch already has a state observer")
		}
		if req.ConnectionEpoch > current.epoch {
			current.cancel()
		}
	}
	if req.ConnectionEpoch > s.highestEpoch {
		s.highestEpoch = req.ConnectionEpoch
		s.highestSandboxUID = req.SandboxUid
		s.highestIncarnationUID = req.IncarnationUid
	}
	ctx, cancel := context.WithCancel(parent)
	current := &watch{
		ctx: ctx, cancel: cancel, sandboxUID: req.SandboxUid,
		incarnationUID: req.IncarnationUid, epoch: req.ConnectionEpoch,
	}
	s.watch = current
	return current, nil
}

func (s *Server) finishWatch(current *watch) {
	s.mu.Lock()
	if s.watch == current {
		current.ready = false
		s.watch = nil
	}
	s.mu.Unlock()
	current.cancel()
}

// Authorize checks the authenticated peer and fencing metadata for a direct
// control RPC or executor stream.
func (s *Server) Authorize(ctx context.Context) (*Epoch, error) {
	if err := s.authorizeGatewayPeer(ctx); err != nil {
		return nil, err
	}
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "Runner control metadata is required")
	}
	value := func(name string) (string, bool) {
		return oneMetadataValue(values.Get(name))
	}
	sandboxUID, sandboxOK := value(SandboxMetadata)
	incarnationUID, incarnationOK := value(IncarnationMetadata)
	bootID, bootOK := value(BootMetadata)
	var epoch uint64
	if epochValues := values.Get(EpochMetadata); len(epochValues) != 0 {
		epochText, epochOK := oneMetadataValue(epochValues)
		parsed, err := strconv.ParseUint(epochText, 10, 64)
		if !epochOK || err != nil {
			return nil, status.Error(codes.Unauthenticated, "Runner connection epoch is invalid")
		}
		epoch = parsed
	}
	if !sandboxOK || !incarnationOK || !bootOK {
		return nil, status.Error(codes.Unauthenticated, "Runner control metadata is incomplete or invalid")
	}
	if sandboxUID != s.config.SandboxUID {
		return nil, status.Error(codes.PermissionDenied, "Runner control metadata names another Sandbox")
	}
	if s.config.IsClosing() {
		return nil, status.Error(codes.Unavailable, "Runner is closing")
	}
	s.mu.RLock()
	current := s.watch
	if epoch == 0 {
		current = s.identity
	}
	if current == nil || !current.ready || current.ctx.Err() != nil || current.sandboxUID != sandboxUID ||
		current.incarnationUID != incarnationUID || current.epoch != epoch || bootID != s.config.BootID ||
		(epoch == 0 && s.highestIncarnationUID != "" && s.highestIncarnationUID != incarnationUID) {
		s.mu.RUnlock()
		// A process restart can leave the Gateway briefly holding the previous boot's
		// connection. Reject before admission with a retryable status.
		return nil, status.Error(codes.Unavailable, "Runner control call is outside the current connection epoch")
	}
	s.mu.RUnlock()
	return &Epoch{server: s, watch: current}, nil
}

// Validate rejects an authorization after its incarnation or legacy watch has been superseded.
func (s *Server) Validate(epoch *Epoch) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.validateLocked(epoch)
}

// Admit keeps the authorized incarnation or legacy watch current until the short
// admission callback finishes.
func (s *Server) Admit(ctx context.Context, admit func(*Epoch) error) error {
	epoch, err := s.Authorize(ctx)
	if err != nil {
		return err
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	if err := s.validateLocked(epoch); err != nil {
		return status.Error(codes.Unavailable, "Runner control call is outside the current connection epoch")
	}
	return admit(epoch)
}

func (s *Server) validateLocked(epoch *Epoch) error {
	if epoch == nil || epoch.server != s || epoch.watch == nil {
		return status.Error(codes.Aborted, "executor stream belongs to a stale connection epoch")
	}
	current := s.watch
	if epoch.watch.epoch == 0 {
		current = s.identity
		if s.highestIncarnationUID != "" && s.highestIncarnationUID != epoch.watch.incarnationUID {
			return status.Error(codes.Aborted, "control authorization belongs to a stale incarnation")
		}
	}
	if current != epoch.watch || !epoch.watch.ready || epoch.watch.ctx.Err() != nil {
		return status.Error(codes.Aborted, "executor stream belongs to a stale connection epoch")
	}
	return nil
}

func (s *Server) authorizeGatewayPeer(ctx context.Context) error {
	if err := s.config.Security.AuthorizePeer(ctx, environmentruntimetransport.GatewayURI); err != nil {
		if err := s.config.Security.AuthorizePeer(ctx, environmentruntimetransport.RuntimeProxyURI); err != nil {
			return status.Error(codes.Unauthenticated, "Runtime control peer is not an admitted Runtime proxy")
		}
	}
	return nil
}

func oneMetadataValue(values []string) (string, bool) {
	if len(values) != 1 || values[0] == "" {
		return "", false
	}
	return values[0], true
}

func (s *Server) WatchState(
	request *environmentruntimepb.WatchStateRequest,
	server grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse],
) error {
	if request == nil {
		return status.Error(codes.InvalidArgument, "watch request is required")
	}
	authorize := s.authorizeGatewayPeer
	if request.ObserveOnly {
		authorize = s.authorizeControllerPeer
	}
	if err := authorize(server.Context()); err != nil {
		return err
	}
	var current *watch
	var err error
	if request.ObserveOnly {
		current, err = s.passiveWatchContext(request, server.Context())
	} else {
		current, err = s.directWatchContext(request, server.Context())
	}
	if err != nil {
		return err
	}
	if request.ObserveOnly || request.ConnectionEpoch == 0 {
		defer current.cancel()
	} else {
		defer s.finishWatch(current)
	}
	if request.ObserveOnly {
		if err := s.bindControllerWatch(current); err != nil {
			return err
		}
	} else if request.ConnectionEpoch != 0 {
		if err := s.markWatchReady(current); err != nil {
			return err
		}
	}
	return s.forwardWatchState(
		current.ctx, authorize,
		func() (*environmentruntimepb.WatchStateResponse, error) {
			if request.ObserveOnly || request.ConnectionEpoch == 0 {
				if err := s.validatePassiveWatch(current); err != nil {
					return nil, err
				}
			}
			facts, err := s.config.Facts()
			if err != nil {
				return nil, err
			}
			return &environmentruntimepb.WatchStateResponse{
				SandboxUid: current.sandboxUID, IncarnationUid: current.incarnationUID,
				EnvironmentRuntimeBootId: s.config.BootID, ConnectionEpoch: current.epoch, Facts: facts,
			}, nil
		},
		server.Send,
	)
}

// Passive watches own only their stream context. In particular, finishing one
// cannot clear the Gateway watch or cancel its admission/Executor context.
func (s *Server) passiveWatchContext(req *environmentruntimepb.WatchStateRequest, parent context.Context) (*watch, error) {
	if req.SandboxUid == "" || req.IncarnationUid == "" || req.ConnectionEpoch != 0 {
		return nil, status.Error(codes.InvalidArgument, "observe-only watch requires sandbox UID, incarnation UID, and zero connection epoch")
	}
	if req.SandboxUid != s.config.SandboxUID {
		return nil, status.Error(codes.PermissionDenied, "watch request names another Sandbox")
	}
	ctx, cancel := context.WithCancel(parent)
	return &watch{ctx: ctx, cancel: cancel, sandboxUID: req.SandboxUid, incarnationUID: req.IncarnationUid}, nil
}

func (s *Server) authorizeControllerPeer(ctx context.Context) error {
	if err := s.config.Security.AuthorizePeer(ctx, environmentruntimetransport.SandboxControllerURI); err != nil {
		return status.Error(codes.Unauthenticated, "observe-only peer is not authenticated as the Sandbox Controller")
	}
	return nil
}

func (s *Server) validatePassiveWatch(current *watch) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.boundIncarnationUID != "" && s.boundIncarnationUID != current.incarnationUID {
		return status.Error(codes.FailedPrecondition, "watch cannot replace the Runtime incarnation")
	}
	return nil
}

func (s *Server) bindControllerWatch(current *watch) error {
	// Only the authenticated Controller names this process's workload incarnation.
	// Reserve it before the lifecycle callback: an uncertain adoption cannot let a
	// delayed observer restore another UID. Reconnect may settle only the same UID.
	s.adoptionMu.Lock()
	defer s.adoptionMu.Unlock()
	if err := current.ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	if err := s.validatePassiveWatch(current); err != nil {
		return err
	}
	s.mu.Lock()
	s.boundIncarnationUID = current.incarnationUID
	s.mu.Unlock()
	if err := s.config.AdoptIncarnation(current.ctx, current.incarnationUID); err != nil {
		return err
	}
	s.mu.Lock()
	if s.identity == nil {
		// Observer loss and another Gateway replica do not end identity-bound work.
		// The caller stream and Runtime lifecycle retain their own cancellation.
		ctx, cancel := context.WithCancel(context.Background())
		s.identity = &watch{ctx: ctx, cancel: cancel, sandboxUID: current.sandboxUID, incarnationUID: current.incarnationUID, ready: true}
	}
	s.mu.Unlock()
	if err := current.ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	return nil
}

func (s *Server) forwardWatchState(
	ctx context.Context,
	authorize func(context.Context) error,
	response func() (*environmentruntimepb.WatchStateResponse, error),
	send func(*environmentruntimepb.WatchStateResponse) error,
) error {
	ticker := time.NewTicker(forwardHeartbeatInterval)
	defer ticker.Stop()
	var lastResponse *environmentruntimepb.WatchStateResponse
	var lastStage environmentruntimestatepb.EnvironmentRuntimeFacts_QuiesceStage
	stageSeen := false
	forceHeartbeat := true
	for {
		if err := authorize(ctx); err != nil {
			return err
		}
		changed := s.config.Changes()
		current, err := response()
		if err != nil {
			return err
		}
		stage := current.GetFacts().GetQuiesceStage()
		if !stageSeen || stage != lastStage {
			stageSeen = true
			lastStage = stage
			if stageName, ok := quiesceStageName(stage); ok {
				recordQuiesceStage(ctx, stageName)
				trace.SpanFromContext(ctx).AddEvent(
					"agentplane.environment_runtime.quiesce_stage",
					trace.WithAttributes(attribute.String("agentplane.environment_runtime.quiesce.stage", stageName)),
				)
			}
		}
		if forceHeartbeat || lastResponse == nil || !proto.Equal(lastResponse, current) {
			if err := streaming.SendWithin(ctx, forwardWriteTimeout, send, current); err != nil {
				if errors.Is(err, streaming.ErrSendTimeout) {
					return status.Error(codes.DeadlineExceeded, "Runner state write timed out")
				}
				return err
			}
			lastResponse = current
			forceHeartbeat = false
		}
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-changed:
		case <-ticker.C:
			forceHeartbeat = true
		}
	}
}

func quiesceStageName(stage environmentruntimestatepb.EnvironmentRuntimeFacts_QuiesceStage) (string, bool) {
	switch stage {
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_UNSPECIFIED:
		return "none", true
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_DRAINING:
		return "draining", true
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING:
		return "flushing", true
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED:
		return "quiesced", true
	default:
		return "", false
	}
}

func recordQuiesceStage(ctx context.Context, stage string) {
	_, span := observability.Start(ctx, "agentplane.environment_runtime.quiesce_stage")
	span.SetAttributes(attribute.String("agentplane.environment_runtime.quiesce.stage", stage))
	span.End()
}

func (s *Server) markWatchReady(current *watch) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.watch != current || current.ctx.Err() != nil {
		return status.Error(codes.Aborted, "watch was superseded before becoming ready")
	}
	current.ready = true
	return nil
}

var _ environmentruntimepb.EnvironmentRuntimeServer = (*Server)(nil)
