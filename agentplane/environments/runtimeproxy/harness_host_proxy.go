package runtimeproxy

import (
	"context"
	"io"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
)

type harnessHostProxy struct {
	harnesshostpb.UnimplementedHarnessHostServer
	server *Server
}

func (p *harnessHostProxy) connection(ctx context.Context, writes bool) (*runtimeConnection, error) {
	return (&environmentRuntimeProxy{server: p.server}).connection(ctx, writes, false)
}

func (p *harnessHostProxy) CreateSession(ctx context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.CreateSession(ctx, request)
}

func (p *harnessHostProxy) SubmitInput(ctx context.Context, request *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.SubmitInput(ctx, request)
}

func (p *harnessHostProxy) ChangeModel(ctx context.Context, request *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.ChangeModel(ctx, request)
}

func (p *harnessHostProxy) ChangeReasoningEffort(ctx context.Context, request *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.ChangeReasoningEffort(ctx, request)
}

func (p *harnessHostProxy) InterruptTurn(ctx context.Context, request *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	connection, err := p.connection(ctx, true)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.InterruptTurn(ctx, request)
}

func (p *harnessHostProxy) ListSessions(ctx context.Context, request *harnesshostpb.ListSessionsRequest) (*harnesshostpb.ListSessionsResponse, error) {
	connection, err := p.connection(ctx, false)
	if err != nil {
		return nil, err
	}
	return connection.harnessHost.ListSessions(ctx, request)
}

func (p *harnessHostProxy) WatchSessions(request *harnesshostpb.WatchSessionsRequest, stream harnesshostpb.HarnessHost_WatchSessionsServer) error {
	connection, err := p.connection(stream.Context(), false)
	if err != nil {
		return err
	}
	upstreamContext, cancel := context.WithCancel(stream.Context())
	defer cancel()
	upstream, err := connection.harnessHost.WatchSessions(upstreamContext, request)
	if err != nil {
		return err
	}
	for {
		response, err := upstream.Recv()
		if err != nil {
			select {
			case <-connection.ended:
				return connection.error()
			default:
			}
			if err == io.EOF {
				return nil
			}
			return err
		}
		if err := streaming.SendWithin(stream.Context(), p.server.config.ControlTiming.LivenessTimeout, stream.Send, response); err != nil {
			return err
		}
	}
}

var _ harnesshostpb.HarnessHostServer = (*harnessHostProxy)(nil)
