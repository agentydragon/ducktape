package api

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/oauthprovider"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
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
	listed, err := s.operators.ListMcpServers(ctx, &operator_action_servicepb.ListMcpServersRequest{})
	if err != nil || len(listed.GetLinkages()) != 1 || !proto.Equal(listed.GetLinkages()[0], unlinked) {
		t.Errorf("the listing is %v, %v", listed, err)
	}

	started, err := s.operators.StartMcpServerLinkage(ctx, &operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(started.GetAuthorizationUrl(), provider.URL+"/authorize?") {
		t.Errorf("the authorization URL is %s", started.GetAuthorizationUrl())
	}
	if _, err := s.linkages.Callback(t.Context(), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code"); err != nil {
		t.Fatal(err)
	}
	got, err := s.operators.GetMcpServerLinkage(ctx, &operator_action_servicepb.GetMcpServerLinkageRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	linked := got.GetLinkage()
	if linked.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED || linked.GetRevision() != 1 ||
		!proto.Equal(linked.GetLinkedBy(), &action_servicepb.Operator{Issuer: s.issuer.url, Subject: operatorSubject}) {
		t.Errorf("the linkage is %v", linked)
	}

	disconnected, err := s.operators.DisconnectMcpServer(ctx, &operator_action_servicepb.DisconnectMcpServerRequest{ServerId: testServer})
	if err != nil {
		t.Fatal(err)
	}
	// The configured scopes again, which are none.
	unlinked.Revision = 2
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
	_, err := s.operators.GetMcpServerLinkage(ctx, &operator_action_servicepb.GetMcpServerLinkageRequest{ServerId: "test-unknown"})
	wantCode(t, "the linkage of an unknown server", err, codes.NotFound)
	_, err = s.operators.StartMcpServerLinkage(ctx, &operator_action_servicepb.StartMcpServerLinkageRequest{ServerId: "test-unknown"})
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

func TestTheHTTPFacadeServesTheMCPServers(t *testing.T) {
	provider := oauthprovider.New(t, oauthprovider.Options{Metadata: true})
	s := serveWith(t, kube.TokenReviews(), &catalog.Catalog{}, provider.Server(testServer))
	code, body := s.get(t, "/v1/operator/mcp-servers", s.operatorToken)
	var listed operator_action_servicepb.ListMcpServersResponse
	if err := protojson.Unmarshal(body, &listed); code != http.StatusOK || err != nil || len(listed.GetLinkages()) != 1 ||
		listed.GetLinkages()[0].GetServerId() != testServer {
		t.Errorf("the listing answered %d %s", code, body)
	}
	if code, body := s.get(t, "/v1/operator/mcp-servers/test-unknown/linkage", s.operatorToken); code != http.StatusNotFound {
		t.Errorf("the linkage of an unknown server answered %d %s", code, body)
	}
	if code, body := s.get(t, "/v1/operator/mcp-servers", callerToken); code != http.StatusUnauthorized {
		t.Errorf("a workload's listing answered %d %s", code, body)
	}
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
	// No commit marks the expiry: the watch sends it once the clock reaches expires_at.
	if upsert := nextUpsert(t, "the expiry", stream); upsert.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_EXPIRED ||
		upsert.GetRevision() != linked.GetRevision() {
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
