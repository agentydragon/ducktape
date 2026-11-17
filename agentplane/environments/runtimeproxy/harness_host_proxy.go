package runtimeproxy

import (
	"context"
	"io"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
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
	var header metadata.MD
	response, err := connection.harnessHost.ListSessions(ctx, request, grpc.Header(&header))
	if err != nil {
		return nil, err
	}
	if err := harnesshost.RequireSessionVersion(header); err != nil {
		return nil, err
	}
	if err := grpc.SetHeader(ctx, metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return nil, err
	}
	return response, nil
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
	// Header reads no protobuf frames. Legacy SessionSummary fields collide with
	// Session, so reject old Runtimes before the first Recv can decode their data.
	header, err := upstream.Header()
	// Cancellation can return an empty header without an error. Connection loss
	// remains an incarnation-fence error, not an unsupported-version diagnosis.
	select {
	case <-connection.ended:
		return connection.error()
	default:
	}
	if err != nil {
		return err
	}
	if err := harnesshost.RequireSessionVersion(header); err != nil {
		return err
	}
	if err := stream.SendHeader(metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
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
