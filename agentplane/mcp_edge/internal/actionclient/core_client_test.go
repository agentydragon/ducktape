package actionclient_test

import (
	"context"
	"crypto/x509"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/actionclient"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/connections"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/google/uuid"
	mcpauth "github.com/modelcontextprotocol/go-sdk/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	testConnectionID = "018f4e32-6632-7e7f-83dd-7f90ccf84f68"
	testGrantID      = "018f4e32-6632-7e7f-83dd-7f90ccf84f69"
	projectedToken   = "projected-action-audience-token"
)

var (
	testIdentity = auth.Identity{Name: "mcp-subject", UID: "uid-mcp-subject"}
	testGrant    = connections.ResolvedGrant{
		ConnectionID: uuid.MustParse(testConnectionID), GrantID: uuid.MustParse(testGrantID), Identity: testIdentity,
	}
)

type watchActionServer struct {
	pb.UnimplementedActionServiceServer
	started chan metadata.MD
}

func (s *watchActionServer) WatchActionRequest(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.started <- md
	if err := stream.Send(&pb.WatchActionRequestResponse{}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func startActionServer(t *testing.T, options ...grpc.ServerOption) (string, <-chan metadata.MD) {
	t.Helper()
	server := grpc.NewServer(options...)
	started := make(chan metadata.MD, 1)
	pb.RegisterActionServiceServer(server, &watchActionServer{started: started})
	return grpcserver.Serve(t, server), started
}

func wantDelegation(t *testing.T, md metadata.MD) {
	t.Helper()
	delegations := md.Get(auth.DelegationMetadataKey)
	if len(delegations) != 1 {
		t.Fatalf("delegation metadata values %d, want one", len(delegations))
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal([]byte(delegations[0]), envelope); err != nil {
		t.Fatal(err)
	}
	if got := envelope.GetIdentity(); got == nil || got.GetName() != testIdentity.Name || got.GetUid() != testIdentity.UID {
		t.Fatalf("effective Identity %v", got)
	}
	provenance := envelope.GetConnection()
	if provenance == nil || provenance.GetConnectionId() != testConnectionID || provenance.GetGrantId() != testGrantID {
		t.Fatalf("Connection provenance %v", provenance)
	}
}

func TestConnectionDelegationUsesProjectedActorTokenAndTypedProvenance(t *testing.T) {
	target, started := startActionServer(t)
	tokenPath := filepath.Join(t.TempDir(), "action-token")
	if err := os.WriteFile(tokenPath, []byte(projectedToken), 0o600); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	revoked := false
	client, err := actionclient.New(actionclient.Config{
		ResolveGrant: func(_ context.Context, bearer string) (*connections.ResolvedGrant, error) {
			mu.Lock()
			defer mu.Unlock()
			if bearer != "external-connection-token" || revoked {
				return nil, mcpauth.ErrInvalidToken
			}
			grant := testGrant
			return &grant, nil
		},
		RecheckPeriod: 10 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	clientConn, err := client.Dial(target, tokenPath, time.Second, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = clientConn.Close() })

	ctx := actionclient.WithBearer(t.Context(), "external-connection-token")
	watch, err := pb.NewActionServiceClient(clientConn).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("first Action frame: %v", err)
	}
	md := <-started
	if got := md.Get("authorization"); len(got) != 1 || got[0] != auth.DelegatedScheme+" "+projectedToken {
		t.Fatalf("Action actor credentials %q", got)
	}
	wantDelegation(t, md)

	mu.Lock()
	revoked = true
	mu.Unlock()
	if _, err := watch.Recv(); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("revoked Connection left its Action stream open: %v", err)
	}
}

func TestConnectionDelegationRefusesInvalidBearerWithoutActorFallback(t *testing.T) {
	target, started := startActionServer(t)
	tokenPath := filepath.Join(t.TempDir(), "action-token")
	if err := os.WriteFile(tokenPath, []byte(projectedToken), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := actionclient.New(actionclient.Config{ResolveGrant: func(context.Context, string) (*connections.ResolvedGrant, error) {
		return nil, errors.Join(mcpauth.ErrInvalidToken, errors.New("revoked grant"))
	}})
	if err != nil {
		t.Fatal(err)
	}
	clientConn, err := client.Dial(target, tokenPath, time.Second, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = clientConn.Close() })
	ctx := actionclient.WithBearer(t.Context(), "revoked-token")
	_, err = pb.NewActionServiceClient(clientConn).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid Connection bearer: %v", err)
	}
	select {
	case <-started:
		t.Fatal("Action received a call after the Connection bearer was refused")
	default:
	}
}

func TestConnectionDelegationUsesMCPServiceSVIDWithoutBearerFallback(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	clientCertificate := ca.IssueWithUsage(t, peertls.MCPEdgeURI, x509.ExtKeyUsageClientAuth)
	serverCertificate := ca.IssueWithUsage(t, peertls.ActionsURI, x509.ExtKeyUsageServerAuth)
	serverCredentials, err := serverCertificate.ServerCredentials(peertls.MCPEdgeURI)
	if err != nil {
		t.Fatal(err)
	}
	target, started := startActionServer(t, grpc.Creds(serverCredentials))
	transport := &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.ActionsURI,
		CertFile: clientCertificate.CertFile, KeyFile: clientCertificate.KeyFile, ClientURI: peertls.MCPEdgeURI,
	}
	client, err := actionclient.New(actionclient.Config{ResolveGrant: func(_ context.Context, bearer string) (*connections.ResolvedGrant, error) {
		if bearer != "external-connection-token" {
			return nil, mcpauth.ErrInvalidToken
		}
		grant := testGrant
		return &grant, nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	clientConn, err := client.Dial(target, "", time.Second, transport)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = clientConn.Close() })

	ctx := actionclient.WithBearer(t.Context(), "external-connection-token")
	watch, err := pb.NewActionServiceClient(clientConn).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("first Action frame: %v", err)
	}
	md := <-started
	if got := md.Get("authorization"); len(got) != 0 {
		t.Fatalf("service SVID call included authorization metadata %q", got)
	}
	wantDelegation(t, md)
}
