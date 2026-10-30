package gateway

import (
	"context"
	"io"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type executorServer struct {
	executorpb.UnimplementedExecutorServer
	gateway *Gateway
}

// Resolve each request through the current Sandbox and its elected Gateway-to-Runner connection. Gateway.Methods
// admitted the caller.
func (s *executorServer) executionConnection(
	ctx context.Context,
	scope *executorpb.ExecutionScope,
	newSession bool,
) (context.Context, *runnerConnection, context.CancelFunc, error) {
	g := s.gateway
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
	s *executorServer,
	ctx context.Context,
	scope *executorpb.ExecutionScope,
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
func (s *executorServer) OpenSession(
	request *executorpb.OpenSessionRequest,
	stream grpc.ServerStreamingServer[executorpb.OpenSessionResponse],
) error {
	ctx, connection, stop, err := s.executionConnection(stream.Context(), request.GetScope(), true)
	if err != nil {
		return err
	}
	defer stop()
	private, err := executorpb.NewExecutorClient(connection.direct).OpenSession(ctx, request)
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

func (s *executorServer) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.StartProcessResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).StartProcess(ctx, request)
	})
}

func (s *executorServer) ReadProcess(ctx context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.ReadProcessResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).ReadProcess(ctx, request)
	})
}

func (s *executorServer) WriteProcess(ctx context.Context, request *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.WriteProcessResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).WriteProcess(ctx, request)
	})
}

func (s *executorServer) SignalProcess(ctx context.Context, request *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.SignalProcessResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).SignalProcess(ctx, request)
	})
}

func (s *executorServer) TerminateProcess(ctx context.Context, request *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.TerminateProcessResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).TerminateProcess(ctx, request)
	})
}

func (s *executorServer) GetFileMetadata(ctx context.Context, request *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.GetFileMetadataResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).GetFileMetadata(ctx, request)
	})
}

func (s *executorServer) OpenFile(ctx context.Context, request *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.OpenFileResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).OpenFile(ctx, request)
	})
}

func (s *executorServer) ReadFile(ctx context.Context, request *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.ReadFileResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).ReadFile(ctx, request)
	})
}

func (s *executorServer) CloseFile(ctx context.Context, request *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error) {
	return forwardExecution(s, ctx, request.GetScope(), func(ctx context.Context, connection *runnerConnection) (*executorpb.CloseFileResponse, error) {
		return executorpb.NewExecutorClient(connection.direct).CloseFile(ctx, request)
	})
}

var _ executorpb.ExecutorServer = (*executorServer)(nil)
