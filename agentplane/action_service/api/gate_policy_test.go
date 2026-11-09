package api

import (
	"context"
	"errors"
	"io"
	"net"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/protobuf/types/known/emptypb"
)

func TestGateExplicitlyClassifiesEveryRegisteredMethod(t *testing.T) {
	connectionMethods := grpcmethods.Of(connection_servicepb.ConnectionService_ServiceDesc)
	additionalMethods := make([]string, len(connectionMethods))
	for i, method := range connectionMethods {
		additionalMethods[i] = method.Name
	}
	gate := NewGate(GateOptions{AdditionalOperatorMethods: additionalMethods})
	for _, tc := range []struct {
		name      string
		desc      grpc.ServiceDesc
		protected bool
	}{
		{"workload Action API", action_servicepb.ActionService_ServiceDesc, true},
		{"operator API", operator_action_servicepb.OperatorActionService_ServiceDesc, true},
		{"Web Push API", web_push_servicepb.WebPushService_ServiceDesc, true},
		{"MCP Connection API", connection_servicepb.ConnectionService_ServiceDesc, true},
		{"health API", healthpb.Health_ServiceDesc, false},
		{"linkage API", linkage_servicepb.LinkageService_ServiceDesc, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, method := range grpcmethods.Of(tc.desc) {
				authenticate, ok := gate.authenticator(method.Name)
				if !ok || (authenticate != nil) != tc.protected {
					t.Errorf("%s has policy (authenticate=%t, listed=%t), want authenticate=%t and listed", method.Name, authenticate != nil, ok, tc.protected)
				}
			}
		})
	}
	if _, ok := gate.authenticator("/test.unregistered.Service/Call"); ok {
		t.Fatal("an unregistered method was admitted")
	}
}

type unlistedTestService interface{}

type unlistedTestImplementation struct {
	calledUnary  atomic.Bool
	calledStream atomic.Bool
}

var unlistedTestServiceDesc = grpc.ServiceDesc{
	ServiceName: "test.unlisted.Service",
	HandlerType: (*unlistedTestService)(nil),
	Methods: []grpc.MethodDesc{{
		MethodName: "Call",
		Handler: func(server any, ctx context.Context, decode func(any) error, interceptor grpc.UnaryServerInterceptor) (any, error) {
			request := new(emptypb.Empty)
			if err := decode(request); err != nil {
				return nil, err
			}
			handler := func(context.Context, any) (any, error) {
				server.(*unlistedTestImplementation).calledUnary.Store(true)
				return &emptypb.Empty{}, nil
			}
			if interceptor == nil {
				return handler(ctx, request)
			}
			return interceptor(ctx, request, &grpc.UnaryServerInfo{Server: server, FullMethod: "/test.unlisted.Service/Call"}, handler)
		},
	}},
	Streams: []grpc.StreamDesc{{
		StreamName:    "Watch",
		ServerStreams: true,
		Handler: func(server any, stream grpc.ServerStream) error {
			server.(*unlistedTestImplementation).calledStream.Store(true)
			return stream.SendMsg(&emptypb.Empty{})
		},
	}},
}

func TestGateDeniesUnlistedRegisteredMethodsBeforeTheirHandlers(t *testing.T) {
	implementation := &unlistedTestImplementation{}
	gate := NewGate(GateOptions{})
	server := grpc.NewServer(grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
	server.RegisterService(&unlistedTestServiceDesc, implementation)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()

	var response emptypb.Empty
	err = connection.Invoke(ctx, "/test.unlisted.Service/Call", &emptypb.Empty{}, &response)
	wantCode(t, "an unlisted registered unary method", err, codes.PermissionDenied)
	stream, err := connection.NewStream(ctx, &grpc.StreamDesc{ServerStreams: true}, "/test.unlisted.Service/Watch")
	if err != nil {
		wantCode(t, "an unlisted registered server stream", err, codes.PermissionDenied)
	} else {
		sendErr := stream.SendMsg(&emptypb.Empty{})
		if sendErr == nil {
			sendErr = stream.CloseSend()
		}
		if sendErr == nil || errors.Is(sendErr, io.EOF) {
			sendErr = stream.RecvMsg(&response)
		}
		wantCode(t, "an unlisted registered server stream", sendErr, codes.PermissionDenied)
	}
	if implementation.calledUnary.Load() || implementation.calledStream.Load() {
		t.Fatalf("unlisted handlers ran: unary=%t stream=%t", implementation.calledUnary.Load(), implementation.calledStream.Load())
	}
}

func TestHealthAndSingleUseLinkageMethodsReachTheirOwnHandlersWithoutBearer(t *testing.T) {
	s := serve(t)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	health, err := s.health.Check(ctx, &healthpb.HealthCheckRequest{})
	if err != nil || health.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("unauthenticated health check returned %v, %v", health, err)
	}
	_, err = s.linkageRPC.CompleteCallback(ctx, &linkage_servicepb.CompleteCallbackRequest{})
	wantCode(t, "the public linkage callback", err, codes.InvalidArgument)
	_, err = s.linkageRPC.GetClientMetadata(ctx, &linkage_servicepb.GetClientMetadataRequest{})
	wantCode(t, "the public metadata document", err, codes.NotFound)
}
