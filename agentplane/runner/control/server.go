package control

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	runner_execpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
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
	BootMetadata        = "agentplane-runner-boot-id"
	EpochMetadata       = "agentplane-connection-epoch"

	forwardHeartbeatInterval = 10 * time.Second
	forwardWriteTimeout      = 10 * time.Second
)

type Config struct {
	SandboxUID    string
	BootID        string
	ListenAddress string
	Security      runnertransport.Security

	IsClosing        func() bool
	AdoptIncarnation func(context.Context, string) error
	Facts            func() (*runnerstatepb.RunnerFacts, error)
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

// Epoch is an opaque authorization result tied to one active state watch.
type Epoch struct {
	server *Server
	watch  *watch
}

func (e *Epoch) Context() context.Context { return e.watch.ctx }

func (e *Epoch) Number() uint64 { return e.watch.epoch }

func (e *Epoch) SandboxUID() string { return e.watch.sandboxUID }

func (e *Epoch) IncarnationUID() string { return e.watch.incarnationUID }

type Server struct {
	runnerservicepb.RunnerServer

	config Config

	mu                    sync.RWMutex
	watch                 *watch
	highestEpoch          uint64
	highestSandboxUID     string
	highestIncarnationUID string
}

func New(config Config, backend runnerservicepb.RunnerServer) *Server {
	return &Server{config: config, RunnerServer: backend}
}

// Listen opens the configured authenticated control listener and registers the
// Runner, executor stream, and standard gRPC health services.
func (s *Server) Listen(executor runner_execpb.RunnerExecServer) (net.Listener, *grpc.Server, error) {
	credentials, err := s.config.Security.ServerCredentials(runnertransport.GatewayURI)
	if err != nil {
		return nil, nil, err
	}
	listener, err := net.Listen("tcp", s.config.ListenAddress)
	if err != nil {
		return nil, nil, err
	}
	server := grpc.NewServer(
		grpc.Creds(credentials),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			if strings.HasPrefix(info.FullMethod, "/grpc.health.v1.Health/") {
				return handler(ctx, request)
			}
			if _, err := s.Authorize(ctx); err != nil {
				return nil, err
			}
			return handler(ctx, request)
		}),
	)
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(runnerservicepb.Runner_ServiceDesc.ServiceName, grpc_health_v1.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(runner_execpb.RunnerExec_ServiceDesc.ServiceName, grpc_health_v1.HealthCheckResponse_SERVING)
	grpc_health_v1.RegisterHealthServer(server, healthServer)
	runnerservicepb.RegisterRunnerServer(server, s)
	runner_execpb.RegisterRunnerExecServer(server, executor)
	return listener, server, nil
}

func (s *Server) directWatchContext(req *runnerservicepb.WatchStateRequest, parent context.Context) (*watch, error) {
	if req == nil || req.SandboxUid == "" || req.IncarnationUid == "" || req.ConnectionEpoch == 0 {
		return nil, status.Error(codes.InvalidArgument, "sandbox UID, incarnation UID, and nonzero connection epoch are required")
	}
	if req.SandboxUid != s.config.SandboxUID {
		return nil, status.Error(codes.PermissionDenied, "watch request names another Sandbox")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
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
	epochText, epochOK := value(EpochMetadata)
	epoch, err := strconv.ParseUint(epochText, 10, 64)
	if !sandboxOK || !incarnationOK || !bootOK || !epochOK || err != nil || epoch == 0 {
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
	if current == nil || !current.ready || current.ctx.Err() != nil || current.sandboxUID != sandboxUID ||
		current.incarnationUID != incarnationUID || current.epoch != epoch || bootID != s.config.BootID {
		s.mu.RUnlock()
		// A process restart can leave the Gateway briefly holding the previous boot's
		// connection. Reject before admission with a retryable status.
		return nil, status.Error(codes.Unavailable, "Runner control call is outside the current connection epoch")
	}
	s.mu.RUnlock()
	return &Epoch{server: s, watch: current}, nil
}

// Validate rejects an authorization after its state watch has been superseded.
func (s *Server) Validate(epoch *Epoch) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.validateLocked(epoch)
}

// Admit keeps the authorized epoch current until the short admission callback finishes. A newer
// Gateway observer cannot replace the watch between validation and publication of admitted work.
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
	if s.watch != epoch.watch || !epoch.watch.ready || epoch.watch.ctx.Err() != nil {
		return status.Error(codes.Aborted, "executor stream belongs to a stale connection epoch")
	}
	return nil
}

func (s *Server) authorizeGatewayPeer(ctx context.Context) error {
	if err := s.config.Security.AuthorizePeer(ctx, runnertransport.GatewayURI); err != nil {
		return status.Error(codes.Unauthenticated, "Runner control peer is not authenticated as the Runner Gateway")
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
	request *runnerservicepb.WatchStateRequest,
	server grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse],
) error {
	if err := s.authorizeGatewayPeer(server.Context()); err != nil {
		return err
	}
	current, err := s.directWatchContext(request, server.Context())
	if err != nil {
		return err
	}
	defer s.finishWatch(current)
	if err := s.config.AdoptIncarnation(current.ctx, current.incarnationUID); err != nil {
		return err
	}
	if err := current.ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	if err := s.markWatchReady(current); err != nil {
		return err
	}
	return s.forwardWatchState(
		current.ctx,
		func() (*runnerservicepb.WatchStateResponse, error) {
			facts, err := s.config.Facts()
			if err != nil {
				return nil, err
			}
			return &runnerservicepb.WatchStateResponse{
				SandboxUid: current.sandboxUID, IncarnationUid: current.incarnationUID,
				RunnerBootId: s.config.BootID, ConnectionEpoch: current.epoch, Facts: facts,
			}, nil
		},
		server.Send,
	)
}

func (s *Server) forwardWatchState(
	ctx context.Context,
	response func() (*runnerservicepb.WatchStateResponse, error),
	send func(*runnerservicepb.WatchStateResponse) error,
) error {
	ticker := time.NewTicker(forwardHeartbeatInterval)
	defer ticker.Stop()
	var lastResponse *runnerservicepb.WatchStateResponse
	forceHeartbeat := true
	for {
		if err := s.authorizeGatewayPeer(ctx); err != nil {
			return err
		}
		changed := s.config.Changes()
		current, err := response()
		if err != nil {
			return err
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

func (s *Server) markWatchReady(current *watch) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.watch != current || current.ctx.Err() != nil {
		return status.Error(codes.Aborted, "watch was superseded before becoming ready")
	}
	current.ready = true
	return nil
}

var _ runnerservicepb.RunnerServer = (*Server)(nil)
