package api

import (
	"context"
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
	"google.golang.org/grpc/interop/grpc_testing"
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

type unlistedTestImplementation struct {
	grpc_testing.UnimplementedTestServiceServer
	calledUnary  atomic.Bool
	calledStream atomic.Bool
}

func (s *unlistedTestImplementation) UnaryCall(context.Context, *grpc_testing.SimpleRequest) (*grpc_testing.SimpleResponse, error) {
	s.calledUnary.Store(true)
	return &grpc_testing.SimpleResponse{}, nil
}

func (s *unlistedTestImplementation) StreamingOutputCall(_ *grpc_testing.StreamingOutputCallRequest, stream grpc_testing.TestService_StreamingOutputCallServer) error {
	s.calledStream.Store(true)
	return stream.Send(&grpc_testing.StreamingOutputCallResponse{})
}

func TestGateDeniesUnlistedRegisteredMethodsBeforeTheirHandlers(t *testing.T) {
	implementation := &unlistedTestImplementation{}
	gate := NewGate(GateOptions{})
	server := grpc.NewServer(grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
	grpc_testing.RegisterTestServiceServer(server, implementation)
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

	client := grpc_testing.NewTestServiceClient(connection)
	_, err = client.UnaryCall(ctx, &grpc_testing.SimpleRequest{})
	wantCode(t, "an unlisted registered unary method", err, codes.PermissionDenied)
	stream, err := client.StreamingOutputCall(ctx, &grpc_testing.StreamingOutputCallRequest{})
	if err != nil {
		wantCode(t, "an unlisted registered server stream", err, codes.PermissionDenied)
	} else {
		_, err = stream.Recv()
		wantCode(t, "an unlisted registered server stream", err, codes.PermissionDenied)
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
