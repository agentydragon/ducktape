package sessionclient

import (
	"context"
	"errors"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type diagnosticEnvironmentService struct {
	environmentpb.UnimplementedEnvironmentServiceServer
	t        *testing.T
	response *environmentpb.GetRuntimeDiagnosticsResponse
	err      error
	calls    int
}

func (s *diagnosticEnvironmentService) GetRuntimeDiagnostics(ctx context.Context, request *environmentpb.GetRuntimeDiagnosticsRequest) (*environmentpb.GetRuntimeDiagnosticsResponse, error) {
	s.t.Helper()
	uri, err := peertls.PeerIdentity(ctx)
	if err != nil || uri != peertls.AppURI {
		s.t.Fatalf("diagnostic peer = %q, %v", uri, err)
	}
	md, _ := metadata.FromIncomingContext(ctx)
	for _, key := range []string{"authorization", "x-agentplane-sandbox", "x-agentplane-sandbox-uid"} {
		if len(md.Get(key)) != 0 {
			s.t.Errorf("diagnostic call leaked %s", key)
		}
	}
	if _, ok := ctx.Deadline(); !ok {
		s.t.Error("diagnostic read has no deadline")
	}
	if !proto.Equal(request.GetTarget(), s.response.GetTarget()) {
		s.t.Errorf("diagnostic target = %v", request.GetTarget())
	}
	s.calls++
	return s.response, s.err
}

func TestRuntimeDiagnosticsUsesEnvironmentServiceSVIDAndFailsClosed(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	serving := ca.Issue(t, peertls.SandboxControllerURI)
	credentials, err := serving.ServerCredentials(peertls.AppURI)
	if err != nil {
		t.Fatal(err)
	}
	target := &identitypb.EnvironmentTarget{Namespace: "operators", Name: "environment", Uid: "environment-uid"}
	snapshot := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "runtime-boot", Sessions: []*sessionpb.SessionStatus{{SessionId: "busy-session", Busy: true}}}
	response := &environmentpb.GetRuntimeDiagnosticsResponse{Target: target, IncarnationUid: "pod-uid", ObservedAt: timestamppb.Now(), Status: snapshot}
	service := &diagnosticEnvironmentService{t: t, response: response}
	server := grpc.NewServer(grpc.Creds(credentials))
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	address := grpcserver.Serve(t, server)
	app := ca.Issue(t, peertls.AppURI)
	callerCredentials, err := app.ClientCredentials(peertls.SandboxControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(callerCredentials), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	client, err := NewRuntimeDiagnostics("operators", environmentpb.NewEnvironmentServiceClient(connection), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	descriptor := &environmentpb.EnvironmentDescriptor{Target: target}
	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Bearer foreign", "x-agentplane-sandbox", "other"))
	got, err := client.EnvironmentRuntimeStatus(ctx, descriptor)
	if err != nil || !proto.Equal(got, snapshot) {
		t.Fatalf("diagnostics = %v, %v", got, err)
	}
	for _, code := range []codes.Code{codes.Unavailable, codes.Aborted, codes.NotFound, codes.Unimplemented} {
		service.err = status.Error(code, "diagnostic unavailable")
		_, err := client.EnvironmentRuntimeStatus(ctx, descriptor)
		var failure *ServiceError
		if !errors.As(err, &failure) || failure.Code != code {
			t.Fatalf("status %v = %T %v", code, err, err)
		}
	}
	service.err = status.Error(codes.DeadlineExceeded, "diagnostic deadline")
	_, err = client.EnvironmentRuntimeStatus(ctx, descriptor)
	var timeout *RequestTimeoutError
	if !errors.As(err, &timeout) {
		t.Fatalf("diagnostic deadline = %T %v", err, err)
	}
	service.err = nil
	for _, test := range []struct {
		name   string
		change func(*environmentpb.GetRuntimeDiagnosticsResponse)
	}{
		{"missing incarnation", func(r *environmentpb.GetRuntimeDiagnosticsResponse) { r.IncarnationUid = "" }},
		{"missing observation", func(r *environmentpb.GetRuntimeDiagnosticsResponse) { r.ObservedAt = nil }},
		{"missing boot", func(r *environmentpb.GetRuntimeDiagnosticsResponse) { r.Status.EnvironmentRuntimeBootId = "" }},
		{"missing status", func(r *environmentpb.GetRuntimeDiagnosticsResponse) { r.Status = nil }},
	} {
		t.Run(test.name, func(t *testing.T) {
			service.response = proto.Clone(response).(*environmentpb.GetRuntimeDiagnosticsResponse)
			test.change(service.response)
			if _, err := client.EnvironmentRuntimeStatus(ctx, descriptor); err == nil {
				t.Fatal("incomplete evidence accepted")
			}
		})
	}
	calls := service.calls
	for _, bad := range []*environmentpb.EnvironmentDescriptor{nil, {Target: &identitypb.EnvironmentTarget{Namespace: "foreign", Name: "environment", Uid: "environment-uid"}}, {Target: &identitypb.EnvironmentTarget{Namespace: "operators", Name: "environment"}}} {
		if _, err := client.EnvironmentRuntimeStatus(ctx, bad); err == nil {
			t.Fatalf("invalid target %v accepted", bad)
		}
	}
	if service.calls != calls {
		t.Fatal("invalid target reached EnvironmentService")
	}
}
