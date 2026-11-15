package main

import (
	"context"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/admission"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
)

// connectionHTTPHandler registers the generated HTTP routes directly against the in-process
// ConnectionService. The wrapper applies the same admission gate used by native gRPC calls.
func connectionHTTPHandler(ctx context.Context, service connection_servicepb.ConnectionServiceServer, gate *admission.Gate) (http.Handler, error) {
	mux := runtime.NewServeMux()
	if err := connection_servicepb.RegisterConnectionServiceHandlerServer(ctx, mux, gatedConnectionService{service: service, gate: gate}); err != nil {
		return nil, err
	}
	return mux, nil
}

type gatedConnectionService struct {
	connection_servicepb.UnimplementedConnectionServiceServer
	service connection_servicepb.ConnectionServiceServer
	gate    *admission.Gate
}

func admittedUnary[Request any, Response any](
	gate *admission.Gate,
	method string,
	ctx context.Context,
	request *Request,
	call func(context.Context, *Request) (*Response, error),
) (*Response, error) {
	response, err := gate.UnaryInterceptor(ctx, request, &grpc.UnaryServerInfo{FullMethod: method}, func(ctx context.Context, request any) (any, error) {
		return call(ctx, request.(*Request))
	})
	if err != nil {
		return nil, err
	}
	return response.(*Response), nil
}

func (s gatedConnectionService) ListConnectionIdentities(ctx context.Context, request *connection_servicepb.ListConnectionIdentitiesRequest) (*connection_servicepb.ListConnectionIdentitiesResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_ListConnectionIdentities_FullMethodName, ctx, request, s.service.ListConnectionIdentities)
}

func (s gatedConnectionService) ListConnections(ctx context.Context, request *connection_servicepb.ListConnectionsRequest) (*connection_servicepb.ListConnectionsResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_ListConnections_FullMethodName, ctx, request, s.service.ListConnections)
}

func (s gatedConnectionService) GetConnection(ctx context.Context, request *connection_servicepb.GetConnectionRequest) (*connection_servicepb.GetConnectionResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_GetConnection_FullMethodName, ctx, request, s.service.GetConnection)
}

func (s gatedConnectionService) UpdateConnection(ctx context.Context, request *connection_servicepb.UpdateConnectionRequest) (*connection_servicepb.UpdateConnectionResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_UpdateConnection_FullMethodName, ctx, request, s.service.UpdateConnection)
}

func (s gatedConnectionService) RebindConnection(ctx context.Context, request *connection_servicepb.RebindConnectionRequest) (*connection_servicepb.RebindConnectionResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_RebindConnection_FullMethodName, ctx, request, s.service.RebindConnection)
}

func (s gatedConnectionService) UnbindConnection(ctx context.Context, request *connection_servicepb.UnbindConnectionRequest) (*connection_servicepb.UnbindConnectionResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_UnbindConnection_FullMethodName, ctx, request, s.service.UnbindConnection)
}

func (s gatedConnectionService) PreviewConnectionEnrollment(ctx context.Context, request *connection_servicepb.PreviewConnectionEnrollmentRequest) (*connection_servicepb.PreviewConnectionEnrollmentResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_PreviewConnectionEnrollment_FullMethodName, ctx, request, s.service.PreviewConnectionEnrollment)
}

func (s gatedConnectionService) DecideConnectionEnrollment(ctx context.Context, request *connection_servicepb.DecideConnectionEnrollmentRequest) (*connection_servicepb.DecideConnectionEnrollmentResponse, error) {
	return admittedUnary(s.gate, connection_servicepb.ConnectionService_DecideConnectionEnrollment_FullMethodName, ctx, request, s.service.DecideConnectionEnrollment)
}

func (s gatedConnectionService) WatchConnections(request *connection_servicepb.WatchConnectionsRequest, stream grpc.ServerStreamingServer[connection_servicepb.WatchConnectionsResponse]) error {
	return s.gate.StreamInterceptor(nil, stream, &grpc.StreamServerInfo{
		FullMethod:     connection_servicepb.ConnectionService_WatchConnections_FullMethodName,
		IsServerStream: true,
	}, func(_ any, admitted grpc.ServerStream) error {
		return s.service.WatchConnections(request, contextServerStreamingServer[connection_servicepb.WatchConnectionsResponse]{
			ServerStreamingServer: stream,
			ctx:                   admitted.Context(),
		})
	})
}

type contextServerStreamingServer[Response any] struct {
	grpc.ServerStreamingServer[Response]
	ctx context.Context
}

func (s contextServerStreamingServer[Response]) Context() context.Context { return s.ctx }
