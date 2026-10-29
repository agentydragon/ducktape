package runner

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	controlSandboxMetadata     = "agentplane-sandbox-uid"
	controlIncarnationMetadata = "agentplane-incarnation-uid"
	controlBootMetadata        = "agentplane-runner-boot-id"
	controlEpochMetadata       = "agentplane-connection-epoch"
	forwardHeartbeatInterval   = 10 * time.Second
	forwardWriteTimeout        = 10 * time.Second
	forwardOpenTimeout         = 10 * time.Second
)

type directWatch struct {
	ctx            context.Context
	cancel         context.CancelFunc
	sandboxUID     string
	incarnationUID string
	epoch          uint64
	ready          bool
}

type directExecFrame struct {
	frame *executor_gatewaypb.ExecutorFrame
	done  chan error
}

// forwardServer opens the Runner with its explicitly configured transport security.
func (r *Runner) forwardServer() (net.Listener, *grpc.Server, error) {
	credentials, err := r.config.Control.Security.ServerCredentials(runnertransport.GatewayURI)
	if err != nil {
		return nil, nil, err
	}
	listener, err := net.Listen("tcp", r.config.Control.ListenAddress)
	if err != nil {
		return nil, nil, err
	}
	server := grpc.NewServer(
		grpc.Creds(credentials),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			if strings.HasPrefix(info.FullMethod, "/grpc.health.v1.Health/") {
				return handler(ctx, request)
			}
			if _, err := r.authorizeDirectCall(ctx); err != nil {
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
	runnerservicepb.RegisterRunnerServer(server, r)
	runner_execpb.RegisterRunnerExecServer(server, r)
	return listener, server, nil
}

func (r *Runner) directWatchContext(req *runnerservicepb.WatchStateRequest, parent context.Context) (*directWatch, error) {
	if req == nil || req.SandboxUid == "" || req.IncarnationUid == "" || req.ConnectionEpoch == 0 {
		return nil, status.Error(codes.InvalidArgument, "sandbox UID, incarnation UID, and nonzero connection epoch are required")
	}
	if req.SandboxUid != r.config.Launch.Sandbox.UID {
		return nil, status.Error(codes.PermissionDenied, "watch request names another Sandbox")
	}
	r.directMu.Lock()
	defer r.directMu.Unlock()
	if req.ConnectionEpoch < r.directHighestEpoch {
		return nil, status.Error(codes.Aborted, "watch request has a stale connection epoch")
	}
	if req.ConnectionEpoch == r.directHighestEpoch && r.directHighestEpoch != 0 &&
		(req.SandboxUid != r.directHighestSandboxUID || req.IncarnationUid != r.directHighestIncarnationUID) {
		return nil, status.Error(codes.Aborted, "watch request changed identity without advancing the connection epoch")
	}
	if current := r.directWatch; current != nil {
		if current.epoch == req.ConnectionEpoch {
			return nil, status.Error(codes.AlreadyExists, "connection epoch already has a state observer")
		}
		if req.ConnectionEpoch > current.epoch {
			current.cancel()
		}
	}
	if req.ConnectionEpoch > r.directHighestEpoch {
		r.directHighestEpoch = req.ConnectionEpoch
		r.directHighestSandboxUID = req.SandboxUid
		r.directHighestIncarnationUID = req.IncarnationUid
	}
	ctx, cancel := context.WithCancel(parent)
	watch := &directWatch{
		ctx: ctx, cancel: cancel, sandboxUID: req.SandboxUid,
		incarnationUID: req.IncarnationUid, epoch: req.ConnectionEpoch,
	}
	r.directWatch = watch
	return watch, nil
}

func (r *Runner) finishDirectWatch(watch *directWatch) {
	r.directMu.Lock()
	if r.directWatch == watch {
		watch.ready = false
		r.directWatch = nil
	}
	r.directMu.Unlock()
	watch.cancel()
}

func (r *Runner) authorizeDirectCall(ctx context.Context) (*directWatch, error) {
	if err := r.authorizeGatewayPeer(ctx); err != nil {
		return nil, err
	}
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "Runner control metadata is required")
	}
	value := func(name string) (string, bool) {
		items := values.Get(name)
		return oneMetadataValue(items)
	}
	sandboxUID, sandboxOK := value(controlSandboxMetadata)
	incarnationUID, incarnationOK := value(controlIncarnationMetadata)
	bootID, bootOK := value(controlBootMetadata)
	epochText, epochOK := value(controlEpochMetadata)
	epoch, err := strconv.ParseUint(epochText, 10, 64)
	if !sandboxOK || !incarnationOK || !bootOK || !epochOK || err != nil || epoch == 0 {
		return nil, status.Error(codes.Unauthenticated, "Runner control metadata is incomplete or invalid")
	}
	if sandboxUID != r.config.Launch.Sandbox.UID {
		return nil, status.Error(codes.PermissionDenied, "Runner control metadata names another Sandbox")
	}
	if r.isClosing() {
		return nil, status.Error(codes.Unavailable, "Runner is closing")
	}
	r.directMu.RLock()
	watch := r.directWatch
	if watch == nil || !watch.ready || watch.ctx.Err() != nil || watch.sandboxUID != sandboxUID ||
		watch.incarnationUID != incarnationUID || watch.epoch != epoch || bootID != r.bootID {
		r.directMu.RUnlock()
		return nil, status.Error(codes.Aborted, "Runner control call is outside the current connection epoch")
	}
	r.directMu.RUnlock()
	return watch, nil
}

func (r *Runner) authorizeGatewayPeer(ctx context.Context) error {
	if err := r.config.Control.Security.AuthorizePeer(ctx, runnertransport.GatewayURI); err != nil {
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

func (r *Runner) WatchState(
	request *runnerservicepb.WatchStateRequest,
	server grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse],
) error {
	if err := r.authorizeGatewayPeer(server.Context()); err != nil {
		return err
	}
	watch, err := r.directWatchContext(request, server.Context())
	if err != nil {
		return err
	}
	defer r.finishDirectWatch(watch)
	if err := r.adoptIncarnation(watch.ctx, watch.incarnationUID); err != nil {
		return rpcError(err)
	}
	if err := watch.ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	if err := r.markDirectWatchReady(watch); err != nil {
		return err
	}
	ticker := time.NewTicker(forwardHeartbeatInterval)
	defer ticker.Stop()
	for {
		if err := r.authorizeGatewayPeer(watch.ctx); err != nil {
			return err
		}
		changed := r.changesSince()
		facts, err := r.facts()
		if err != nil {
			return rpcError(err)
		}
		response := &runnerservicepb.WatchStateResponse{
			SandboxUid: watch.sandboxUID, IncarnationUid: watch.incarnationUID,
			RunnerBootId: r.bootID, ConnectionEpoch: watch.epoch, Facts: facts,
		}
		if err := streaming.SendWithin(watch.ctx, forwardWriteTimeout, server.Send, response); err != nil {
			if errors.Is(err, streaming.ErrSendTimeout) {
				return status.Error(codes.DeadlineExceeded, "Runner state write timed out")
			}
			return err
		}
		select {
		case <-watch.ctx.Done():
			return status.FromContextError(watch.ctx.Err()).Err()
		case <-changed:
		case <-ticker.C:
		}
	}
}

func (r *Runner) markDirectWatchReady(watch *directWatch) error {
	r.directMu.Lock()
	defer r.directMu.Unlock()
	if r.directWatch != watch || watch.ctx.Err() != nil {
		return status.Error(codes.Aborted, "watch was superseded before becoming ready")
	}
	watch.ready = true
	return nil
}

func (r *Runner) Connect(
	server grpc.BidiStreamingServer[executor_gatewaypb.ExecutorFrame, executor_gatewaypb.ExecutorFrame],
) error {
	watch, err := r.authorizeDirectCall(server.Context())
	if err != nil {
		return err
	}
	r.directMu.Lock()
	if r.directWatch != watch || !watch.ready || watch.ctx.Err() != nil {
		r.directMu.Unlock()
		return status.Error(codes.Aborted, "executor stream belongs to a stale connection epoch")
	}
	if r.directExecStreams >= maxExecutorStreamsPerRunner {
		r.directMu.Unlock()
		return status.Error(codes.ResourceExhausted, "Runner has too many direct executor streams")
	}
	r.directExecStreams++
	r.directMu.Unlock()
	defer func() {
		r.directMu.Lock()
		r.directExecStreams--
		r.directMu.Unlock()
	}()
	if err := server.SendHeader(metadata.MD{}); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(server.Context())
	stopWatch := context.AfterFunc(watch.ctx, cancel)
	defer func() {
		stopWatch()
		cancel()
	}()
	sendQueue := make(chan directExecFrame, executorQueueDepth)
	sendErrors := make(chan error, 1)
	terminal := make(chan struct{})
	var terminalOnce sync.Once
	reportSendError := func(err error) {
		select {
		case sendErrors <- err:
		default:
		}
	}
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case item := <-sendQueue:
				err := streaming.SendWithin(ctx, forwardWriteTimeout, server.Send, item.frame)
				if item.done != nil {
					item.done <- err
				}
				if err != nil {
					if errors.Is(err, streaming.ErrSendTimeout) {
						reportSendError(status.Error(codes.DeadlineExceeded, "Runner executor write timed out"))
					} else {
						reportSendError(err)
					}
					return
				}
			}
		}
	}()
	queueFrame := func(frame *executor_gatewaypb.ExecutorFrame) error {
		item := directExecFrame{frame: frame}
		endsStream := frame.GetClose() != nil || frame.GetReady() != nil && !frame.GetReady().GetAccepted()
		if endsStream {
			item.done = make(chan error, 1)
		}
		select {
		case sendQueue <- item:
		default:
			err := status.Error(codes.ResourceExhausted, "Runner executor send queue is full")
			reportSendError(err)
			return err
		}
		if endsStream {
			select {
			case err := <-item.done:
				if err != nil {
					return err
				}
			case <-ctx.Done():
				return ctx.Err()
			}
			terminalOnce.Do(func() { close(terminal) })
		}
		return nil
	}
	toExecutor := queueFrame
	executor := r.openExecutorConnection(ctx, watch.epoch, toExecutor, toExecutor)
	defer executor.Close()
	type incoming struct {
		frame *executor_gatewaypb.ExecutorFrame
		err   error
	}
	received := make(chan incoming, 1)
	go func() {
		for {
			frame, err := server.Recv()
			select {
			case received <- incoming{frame: frame, err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	opened := false
	openTimer := time.NewTimer(forwardOpenTimeout)
	defer openTimer.Stop()
	for {
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-terminal:
			return nil
		case <-openTimer.C:
			if !opened {
				return status.Error(codes.DeadlineExceeded, "Runner executor stream did not open a channel")
			}
		case err := <-sendErrors:
			return err
		case message := <-received:
			if message.err != nil {
				return message.err
			}
			if err := r.authorizeDirectStreamEpoch(watch); err != nil {
				return err
			}
			frame := message.frame
			if frame == nil || frame.GetFrame() == nil {
				return status.Error(codes.InvalidArgument, "direct executor frames must contain a frame")
			}
			if !opened {
				if frame.GetOpen() == nil {
					return status.Error(codes.InvalidArgument, "direct executor stream must start with ExecutorOpen")
				}
				opened = true
				openTimer.Stop()
			} else if frame.GetOpen() != nil || (frame.GetMessage() == nil && frame.GetClose() == nil) {
				return status.Error(codes.InvalidArgument, "only executor messages and close frames follow ExecutorOpen")
			}
			if message := frame.GetMessage(); message != nil {
				if len(message.GetPayload()) > maxExecutorMessageBytes {
					return status.Error(codes.ResourceExhausted, "executor message exceeds the size limit")
				}
				if message.GetOpcode() != executor_gatewaypb.ExecutorMessage_OPCODE_TEXT && message.GetOpcode() != executor_gatewaypb.ExecutorMessage_OPCODE_BINARY {
					return status.Error(codes.InvalidArgument, "unknown executor message opcode")
				}
			}
			if err := executor.Route(frame); err != nil {
				return err
			}
			if frame.GetClose() != nil {
				return nil
			}
		}
	}
}

func (r *Runner) authorizeDirectStreamEpoch(watch *directWatch) error {
	r.directMu.RLock()
	defer r.directMu.RUnlock()
	if r.directWatch != watch || !watch.ready || watch.ctx.Err() != nil {
		return status.Error(codes.Aborted, "executor stream belongs to a stale connection epoch")
	}
	return nil
}

var (
	_ runnerservicepb.RunnerServer   = (*Runner)(nil)
	_ runner_execpb.RunnerExecServer = (*Runner)(nil)
)
