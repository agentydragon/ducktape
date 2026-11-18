package grpcgate

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/web_push/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/interop/grpc_testing"
	"google.golang.org/grpc/status"
)

func TestGateExplicitlyClassifiesEveryRegisteredMethod(t *testing.T) {
	gate := New(Options{ErrorMapper: func(_ context.Context, err error) error { return err }})
	for _, tc := range []struct {
		name      string
		desc      grpc.ServiceDesc
		protected bool
	}{
		{"workload Action API", action_servicepb.ActionService_ServiceDesc, true},
		{"operator API", operator_action_servicepb.OperatorActionService_ServiceDesc, true},
		{"Web Push API", web_push_servicepb.WebPushService_ServiceDesc, true},
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
	gate := New(Options{ErrorMapper: func(_ context.Context, err error) error { return err }})
	server := grpc.NewServer(grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
	grpc_testing.RegisterTestServiceServer(server, implementation)
	serverAddr := grpcserver.Serve(t, server)
	connection, err := grpc.NewClient(serverAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	client := grpc_testing.NewTestServiceClient(connection)
	_, err = client.UnaryCall(ctx, &grpc_testing.SimpleRequest{})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatal(err)
	}
	stream, err := client.StreamingOutputCall(ctx, &grpc_testing.StreamingOutputCallRequest{})
	if err != nil {
		if status.Code(err) != codes.PermissionDenied {
			t.Fatal(err)
		}
	} else {
		_, err = stream.Recv()
		if status.Code(err) != codes.PermissionDenied {
			t.Fatal(err)
		}
	}
	if implementation.calledUnary.Load() || implementation.calledStream.Load() {
		t.Fatalf("unlisted handlers ran: unary=%t stream=%t", implementation.calledUnary.Load(), implementation.calledStream.Load())
	}
}
