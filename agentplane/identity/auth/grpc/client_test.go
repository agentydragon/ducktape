package workloadgrpc_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

const delegatedToken = "projected-action-audience-token"

var connectionDelegation = &delegationpb.Delegation{
	EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: "agent", Uid: "identity-uid"}},
	Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{
		ConnectionId: "018f4e32-6632-7e7f-83dd-7f90ccf84f68",
		GrantId:      "018f4e32-6632-7e7f-83dd-7f90ccf84f69",
	}},
}

type metadataHealth struct {
	healthpb.UnimplementedHealthServer
	checks  chan metadata.MD
	watches chan metadata.MD
}

func (s metadataHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	s.checks <- md
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (s metadataHealth) Watch(_ *healthpb.HealthCheckRequest, stream grpc.ServerStreamingServer[healthpb.HealthCheckResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.watches <- md
	return stream.Send(&healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING})
}

func TestDialDelegatedInterceptorsUseActorProofForUnaryAndStream(t *testing.T) {
	for _, svid := range []bool{false, true} {
		name := "bearer"
		if svid {
			name = "svid"
		}
		t.Run(name, func(t *testing.T) { testDelegatedActor(t, svid) })
	}
}

func testDelegatedActor(t *testing.T, svid bool) {
	seen := metadataHealth{checks: make(chan metadata.MD, 1), watches: make(chan metadata.MD, 1)}
	var transport *peertls.ClientTLS
	var serverOptions []grpc.ServerOption
	if svid {
		ca := peertlstesting.NewCA(t)
		serverConfig := ca.Issue(t, peertls.ActionsURI)
		serverTLS, err := serverConfig.WorkloadServerTLSConfigWithServices(peertls.ActionsURI, false)
		if err != nil {
			t.Fatal(err)
		}
		creds, err := peertls.ServerCredentials(serverTLS)
		if err != nil {
			t.Fatal(err)
		}
		serverOptions = append(serverOptions, grpc.Creds(creds))
		clientConfig := ca.Issue(t, peertls.MCPEdgeURI)
		transport = &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.ActionsURI,
			CertFile: clientConfig.CertFile, KeyFile: clientConfig.KeyFile, ClientURI: peertls.MCPEdgeURI,
		}
	}
	server := grpc.NewServer(serverOptions...)
	healthpb.RegisterHealthServer(server, seen)
	address := grpcserver.Serve(t, server)

	tokenPath := filepath.Join(t.TempDir(), "action-token")
	if err := os.WriteFile(tokenPath, []byte(delegatedToken), 0o600); err != nil {
		t.Fatal(err)
	}
	if svid {
		tokenPath = ""
	}
	attach := func(ctx context.Context) (context.Context, error) {
		return workloadgrpc.WithDelegation(ctx, connectionDelegation)
	}
	connection, err := workloadgrpc.DialDelegated(address, tokenPath, time.Second, transport,
		workloadgrpc.ClientInterceptors{
			Unary: func(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
				delegated, err := attach(ctx)
				if err != nil {
					return err
				}
				return invoke(delegated, method, request, reply, connection, options...)
			},
			Stream: func(ctx context.Context, description *grpc.StreamDesc, connection *grpc.ClientConn, method string, start grpc.Streamer, options ...grpc.CallOption) (grpc.ClientStream, error) {
				delegated, err := attach(ctx)
				if err != nil {
					return nil, err
				}
				return start(delegated, description, connection, method, options...)
			},
		})
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	client := healthpb.NewHealthClient(connection)
	// The connection's interceptor supplies the envelope for these calls; test
	// the no-interceptor path separately below.
	bare, err := workloadgrpc.DialDelegated(address, tokenPath, time.Second, transport, workloadgrpc.ClientInterceptors{})
	if err != nil {
		t.Fatal(err)
	}
	defer bare.Close()
	if _, err := healthpb.NewHealthClient(bare).Check(t.Context(), &healthpb.HealthCheckRequest{}); err == nil {
		t.Fatal("delegated call without an envelope succeeded")
	}
	if _, err := client.Check(context.Background(), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("delegated unary call: %v", err)
	}
	watch, err := client.Watch(context.Background(), &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatalf("delegated stream call: %v", err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("delegated stream response: %v", err)
	}
	for name, md := range map[string]metadata.MD{"unary": <-seen.checks, "stream": <-seen.watches} {
		if got := md.Get("authorization"); (svid && len(got) != 0) || (!svid && (len(got) != 1 || got[0] != auth.DelegatedScheme+" "+delegatedToken)) {
			t.Errorf("%s authorization = %v", name, got)
		}
		values := md.Get(auth.DelegationMetadataKey)
		if len(values) != 1 {
			t.Errorf("%s delegation envelope values = %d, want one", name, len(values))
			continue
		}
		got := new(delegationpb.Delegation)
		if err := proto.Unmarshal([]byte(values[0]), got); err != nil || !proto.Equal(got, connectionDelegation) {
			t.Errorf("%s delegation = %v, want %v (decode error %v)", name, got, connectionDelegation, err)
		}
	}
}
