package api

import (
	"context"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/oauthprovider"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/proto"
)

const testServer = "test-server"

// link finishes a flow the operator started on the server, as the OAuth provider's redirect to the callback would.
func (s *served) link(t *testing.T, serverID string) *operator_action_servicepb.McpServerLinkage {
	t.Helper()
	started, err := s.operators.StartMcpServerLinkage(as(t, s.operatorToken),
		&operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: serverID})
	if err != nil {
		t.Fatal(err)
	}
	linked, err := s.linkages.Callback(t.Context(), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code")
	if err != nil {
		t.Fatal(err)
	}
	return linked
}

func TestAnOperatorLinksAndDisconnectsAnMCPServer(t *testing.T) {
	provider := oauthprovider.New(t, oauthprovider.Options{Metadata: true, ExpiresIn: 3600})
	s := serveWith(t, kube.TokenReviews(), &catalog.Catalog{}, provider.Server(testServer))
	ctx := as(t, s.operatorToken)
	unlinked := &operator_action_servicepb.McpServerLinkage{
		ServerId: testServer, ServerUrl: provider.URL + "/mcp", Status: operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED,
	}
	stream := s.watchLinkages(t, ctx)
	if snapshot := linkageSnapshot(t, stream); len(snapshot) != 1 || !proto.Equal(snapshot[0], unlinked) {
		t.Errorf("the snapshot is %v", snapshot)
	}

	started, err := s.operators.StartMcpServerLinkage(ctx, &operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(started.GetAuthorizationUrl(), provider.URL+"/authorize?") ||
		!proto.Equal(started, &operator_action_servicepb.StartMcpServerLinkageResponse{AuthorizationUrl: started.GetAuthorizationUrl()}) {
		t.Errorf("the start answered %v", started)
	}
	if _, err := s.linkages.Callback(t.Context(), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code"); err != nil {
		t.Fatal(err)
	}
	// The scopes the server's metadata offers, all of which the link requested.
	linked := &operator_action_servicepb.McpServerLinkage{
		ServerId: testServer, ServerUrl: provider.URL + "/mcp", Status: operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED,
		Scopes: oauthprovider.Scopes,
	}
	if upsert := nextUpsert(t, "the link", stream); !proto.Equal(upsert, linked) {
		t.Errorf("the link's upsert is %v, want %v", upsert, linked)
	}

	disconnected, err := s.operators.DisconnectMcpServer(ctx, &operator_action_servicepb.DisconnectMcpServerRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	// The configured scopes again, which are none.
	if !proto.Equal(disconnected.GetLinkage(), unlinked) {
		t.Errorf("the disconnected linkage is %v", disconnected.GetLinkage())
	}
}

func TestAnMCPServerCallThatCannotBeServedFails(t *testing.T) {
	// The server's metadata names no endpoints, and none are configured.
	provider := oauthprovider.New(t, oauthprovider.Options{})
	unusable := provider.Server(testServer)
	unusable.AuthorizationEndpoint, unusable.TokenEndpoint = nil, nil
	s := serveWith(t, kube.TokenReviews(), &catalog.Catalog{}, unusable)
	ctx := as(t, s.operatorToken)
	_, err := s.operators.StartMcpServerLinkage(ctx, &operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: "test-unknown"})
	wantCode(t, "a link of an unknown server", err, codes.NotFound)
	_, err = s.operators.DisconnectMcpServer(ctx, &operator_action_servicepb.DisconnectMcpServerRequest{ServerId: "test-unknown"})
	wantCode(t, "a disconnection of an unknown server", err, codes.NotFound)
	_, err = s.operators.StartMcpServerLinkage(ctx, &operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: testServer})
	wantCode(t, "a link of a server whose metadata is unusable", err, codes.FailedPrecondition)

	provider = oauthprovider.New(t, oauthprovider.Options{Metadata: true})
	s = serveWith(t, kube.TokenReviews(), &catalog.Catalog{}, provider.Server(testServer))
	_, err = s.operators.StartMcpServerLinkage(as(t, s.operatorToken), &operator_action_servicepb.StartMcpServerLinkageRequest{
		ServerId: testServer, Scopes: []string{"test-read", "test-unconfigured"},
	})
	wantCode(t, "a link with a scope the server is not configured for", err, codes.InvalidArgument)
}

type linkageStream = grpc.ServerStreamingClient[operator_action_servicepb.WatchMcpServersResponse]

func (s *served) watchLinkages(t *testing.T, ctx context.Context) linkageStream {
	t.Helper()
	stream, err := s.operators.WatchMcpServers(ctx, &operator_action_servicepb.WatchMcpServersRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

// linkageSnapshot reads the frames up to snapshot_complete and returns the linkages they carried.
func linkageSnapshot(t *testing.T, stream linkageStream) []*operator_action_servicepb.McpServerLinkage {
	t.Helper()
	var linkages []*operator_action_servicepb.McpServerLinkage
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		switch frame := frame.GetFrame().(type) {
		case *operator_action_servicepb.WatchMcpServersResponse_Snapshot:
			linkages = append(linkages, frame.Snapshot.GetLinkages()...)
		case *operator_action_servicepb.WatchMcpServersResponse_SnapshotComplete:
			return linkages
		default:
			t.Fatalf("a %T frame before snapshot_complete", frame)
		}
	}
}

// nextUpsert reads the next frame, which must be an upsert, and returns the linkage it carries.
func nextUpsert(t *testing.T, what string, stream linkageStream) *operator_action_servicepb.McpServerLinkage {
	t.Helper()
	frame, err := stream.Recv()
	if err != nil {
		t.Fatalf("%s: %v", what, err)
	}
	if frame.GetUpsert() == nil {
		t.Fatalf("%s: frame %v, want an upsert", what, frame)
	}
	return frame.GetUpsert()
}

func TestAnMCPServerWatchSendsEachChangeOfALinkageIncludingItsTokensExpiry(t *testing.T) {
	// Each token lapses a few seconds after it is granted, and is not refreshed.
	provider := oauthprovider.New(t, oauthprovider.Options{Metadata: true, ExpiresIn: 5})
	s := serveWith(t, kube.TokenReviews(), &catalog.Catalog{}, provider.Server(testServer), provider.Server("test-other-server"))
	stream := s.watchLinkages(t, as(t, s.operatorToken))
	if snapshot := linkageSnapshot(t, stream); len(snapshot) != 2 || snapshot[0].GetServerId() != "test-other-server" ||
		snapshot[1].GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED {
		t.Errorf("the snapshot is %v", snapshot)
	}

	linked := s.link(t, testServer)
	if upsert := nextUpsert(t, "the link", stream); !proto.Equal(upsert, linked) ||
		upsert.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED {
		t.Errorf("the link's upsert is %v, want %v", upsert, linked)
	}
	// No commit marks the expiry: the watch sends it once the clock reaches the token's expiry.
	if upsert := nextUpsert(t, "the expiry", stream); upsert.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_EXPIRED ||
		upsert.GetServerId() != testServer {
		t.Errorf("the expiry's upsert is %v", upsert)
	}
	disconnected, err := s.operators.DisconnectMcpServer(as(t, s.operatorToken),
		&operator_action_servicepb.DisconnectMcpServerRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	if upsert := nextUpsert(t, "the disconnection", stream); !proto.Equal(upsert, disconnected.GetLinkage()) {
		t.Errorf("the disconnection's upsert is %v, want %v", upsert, disconnected.GetLinkage())
	}

	s.gate.Drain()
	if frame, err := stream.Recv(); err != nil || frame.GetReconnectRequired() == nil {
		t.Errorf("frame %v, %v; want reconnect_required", frame, err)
	}
	wantEnd(t, "the watch after reconnect_required", stream, codes.OK)
}
