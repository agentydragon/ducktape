package gateway

import (
	"context"
	"io"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type executorGatewayServer struct {
	executorpb.UnimplementedExecutorGatewayServer
	gateway *Gateway
}

// Resolve each request through the current Sandbox and its elected Gateway-to-Runner connection.
func (s *executorGatewayServer) executionConnection(
	ctx context.Context,
	scope *executionpb.ExecutionScope,
	newSession bool,
) (context.Context, *runnerConnection, context.CancelFunc, error) {
	g := s.gateway
	if err := g.authorize(ctx, g.executorCallers); err != nil {
		return nil, nil, nil, err
	}
	if scope == nil || scope.GetDestination() == nil || scope.GetExpectedIncarnationUid() == "" ||
		scope.GetSessionId() == "" || len(scope.GetSessionId()) > 128 {
		return nil, nil, nil, status.Error(codes.InvalidArgument, "destination, expected incarnation and Session ID are required")
	}
	resolveCtx, cancel := context.WithTimeout(ctx, g.config.ControlTiming.LivenessTimeout)
	// Only OpenSession is new admission. Existing-session unary operations remain valid through
	// quiescing and are fenced when Runner drains the held session.
	view, err := g.sandboxes.destination(resolveCtx, scope.GetDestination(), newSession)
	cancel()
	if err != nil {
		return nil, nil, nil, unavailable(err)
	}
	if view.incarnation == nil || scope.GetExpectedIncarnationUid() != view.incarnation.UID {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	connection, err := g.connections.holder(view.identity.UID)
	if err != nil {
		return nil, nil, nil, err
	}
	if connection.identity != view.identity || connection.podUID != scope.GetExpectedIncarnationUid() {
		return nil, nil, nil, status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")
	}
	callCtx, stop := context.WithCancel(ctx)
	go func() {
		select {
		case <-connection.ended:
			stop()
		case <-callCtx.Done():
		}
	}()
	return callCtx, connection, stop, nil
}

func forwardExecution[Response any](
	s *executorGatewayServer,
	ctx context.Context,
	scope *executionpb.ExecutionScope,
	call func(context.Context, *runnerConnection) (*Response, error),
) (*Response, error) {
	callCtx, connection, stop, err := s.executionConnection(ctx, scope, false)
	if err != nil {
		return nil, err
	}
	defer stop()
	return call(callCtx, connection)
}

// OpenSession is the lifetime of one local Codex WebSocket. The Gateway streams every
// initialization/notification response unchanged and closes its Runner stream when the caller
// cancels or the elected Runner connection ends.
func (s *executorGatewayServer) OpenSession(
	request *executionpb.OpenSessionRequest,
	stream grpc.ServerStreamingServer[executionpb.OpenSessionResponse],
) error {
	ctx, connection, stop, err := s.executionConnection(stream.Context(), request.GetScope(), true)
	if err != nil {
		return err
	}
	defer stop()
	private, err := runnerexecpb.NewRunnerExecClient(connection.direct).OpenSession(ctx, request)
	if err != nil {
		return err
	}
	for {
		response, err := private.Recv()
		if err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		if err := streaming.SendWithin(ctx, s.gateway.config.ControlTiming.LivenessTimeout, stream.Send, response); err != nil {
			return err
		}
	}
}

func (s *executorGatewayServer) StartProcess(ctx context.Context, request *executionpb.StartProcessRequest) (*executionpb.StartProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.StartProcessResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).StartProcess(ctx, request)
	})
}

func (s *executorGatewayServer) ReadProcess(ctx context.Context, request *executionpb.ReadProcessRequest) (*executionpb.ReadProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.ReadProcessResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).ReadProcess(ctx, request)
	})
}

func (s *executorGatewayServer) WriteProcess(ctx context.Context, request *executionpb.WriteProcessRequest) (*executionpb.WriteProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.WriteProcessResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).WriteProcess(ctx, request)
	})
}

func (s *executorGatewayServer) SignalProcess(ctx context.Context, request *executionpb.SignalProcessRequest) (*executionpb.SignalProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.SignalProcessResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).SignalProcess(ctx, request)
	})
}

func (s *executorGatewayServer) TerminateProcess(ctx context.Context, request *executionpb.TerminateProcessRequest) (*executionpb.TerminateProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.TerminateProcessResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).TerminateProcess(ctx, request)
	})
}

func (s *executorGatewayServer) GetFileMetadata(ctx context.Context, request *executionpb.GetFileMetadataRequest) (*executionpb.GetFileMetadataResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.GetFileMetadataResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).GetFileMetadata(ctx, request)
	})
}

func (s *executorGatewayServer) OpenFile(ctx context.Context, request *executionpb.OpenFileRequest) (*executionpb.OpenFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.OpenFileResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).OpenFile(ctx, request)
	})
}

func (s *executorGatewayServer) ReadFile(ctx context.Context, request *executionpb.ReadFileRequest) (*executionpb.ReadFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.ReadFileResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).ReadFile(ctx, request)
	})
}

func (s *executorGatewayServer) CloseFile(ctx context.Context, request *executionpb.CloseFileRequest) (*executionpb.CloseFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executionpb.CloseFileResponse, error) {
		return runnerexecpb.NewRunnerExecClient(connection.direct).CloseFile(ctx, request)
	})
}

var _ executorpb.ExecutorGatewayServer = (*executorGatewayServer)(nil)
