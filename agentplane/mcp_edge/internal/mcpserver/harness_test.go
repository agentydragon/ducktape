package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// callerHeader is how the test's core client tells the fake core which caller a call's context carried, standing in
// for whatever credential a real client derives from the auth.TokenInfo.
const callerHeader = "x-test-caller"

// fakeCore answers each unary RPC with unary and each watch with watch, after recording the caller.
type fakeCore struct {
	pb.UnimplementedActionServiceServer
	unary func(ctx context.Context, rpc string, request proto.Message) (proto.Message, error)
	watch func(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error
}

func callerOf(ctx context.Context) string {
	values := metadata.ValueFromIncomingContext(ctx, callerHeader)
	if len(values) != 1 {
		return ""
	}
	return values[0]
}

func answer[Response proto.Message](core *fakeCore, ctx context.Context, rpc string, request proto.Message) (Response, error) {
	var zero Response
	response, err := core.unary(ctx, rpc, request)
	if err != nil {
		return zero, err
	}
	typed, ok := response.(Response)
	if !ok {
		return zero, fmt.Errorf("fake core answered %s with %T", rpc, response)
	}
	return typed, nil
}

func (c *fakeCore) SubmitActionRequest(ctx context.Context, request *pb.SubmitActionRequestRequest) (*pb.SubmitActionRequestResponse, error) {
	return answer[*pb.SubmitActionRequestResponse](c, ctx, "SubmitActionRequest", request)
}

func (c *fakeCore) ListActionRequests(ctx context.Context, request *pb.ListActionRequestsRequest) (*pb.ListActionRequestsResponse, error) {
	return answer[*pb.ListActionRequestsResponse](c, ctx, "ListActionRequests", request)
}

func (c *fakeCore) GetActionRequest(ctx context.Context, request *pb.GetActionRequestRequest) (*pb.GetActionRequestResponse, error) {
	return answer[*pb.GetActionRequestResponse](c, ctx, "GetActionRequest", request)
}

func (c *fakeCore) CancelActionRequest(ctx context.Context, request *pb.CancelActionRequestRequest) (*pb.CancelActionRequestResponse, error) {
	return answer[*pb.CancelActionRequestResponse](c, ctx, "CancelActionRequest", request)
}

func (c *fakeCore) ListActionRequestEvents(ctx context.Context, request *pb.ListActionRequestEventsRequest) (*pb.ListActionRequestEventsResponse, error) {
	return answer[*pb.ListActionRequestEventsResponse](c, ctx, "ListActionRequestEvents", request)
}

func (c *fakeCore) ListActionGroups(ctx context.Context, request *pb.ListActionGroupsRequest) (*pb.ListActionGroupsResponse, error) {
	return answer[*pb.ListActionGroupsResponse](c, ctx, "ListActionGroups", request)
}

func (c *fakeCore) GetAction(ctx context.Context, request *pb.GetActionRequest) (*pb.GetActionResponse, error) {
	return answer[*pb.GetActionResponse](c, ctx, "GetAction", request)
}

func (c *fakeCore) GetActionPolicy(ctx context.Context, request *pb.GetActionPolicyRequest) (*pb.GetActionPolicyResponse, error) {
	return answer[*pb.GetActionPolicyResponse](c, ctx, "GetActionPolicy", request)
}

func (c *fakeCore) WatchActionRequest(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	return c.watch(request, stream)
}

// Bearers the test verifier knows.
const (
	callerToken      = "test-token"
	otherCallerToken = "test-other-token"
	revokedToken     = "test-revoked-token"
	outageToken      = "test-outage-token"
	brokenToken      = "test-broken-token"
)

// verifyTestBearer accepts two callers, refuses a revoked bearer, and stands for an authority outage and a verifier
// fault.
func verifyTestBearer(_ context.Context, token string, _ *http.Request) (*auth.TokenInfo, error) {
	switch token {
	case callerToken:
		return &auth.TokenInfo{UserID: "test-caller"}, nil
	case otherCallerToken:
		return &auth.TokenInfo{UserID: "test-other-caller", Expiration: time.Now().Add(time.Hour)}, nil
	case outageToken:
		return nil, fmt.Errorf("test authority: %w", ErrAuthorityUnavailable)
	case brokenToken:
		return nil, errors.New("test verifier fault")
	}
	return nil, auth.ErrInvalidToken
}

// stampCaller passes the caller a call's context carries to the fake core.
func stampCaller(ctx context.Context) context.Context {
	if info := auth.TokenInfoFromContext(ctx); info != nil {
		return metadata.AppendToOutgoingContext(ctx, callerHeader, info.UserID)
	}
	return ctx
}

// harness is the /mcp handler on a test HTTP server, over a fake core on an in-memory gRPC connection.
type harness struct {
	server *httptest.Server
}

// newHarness serves options, with the test's resource metadata URL and logger.
func newHarness(t *testing.T, core *fakeCore, options Options) *harness {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	grpcServer := grpc.NewServer()
	pb.RegisterActionServiceServer(grpcServer, core)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(grpcServer.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///test-core",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
		grpc.WithUnaryInterceptor(func(ctx context.Context, method string, request, reply any, cc *grpc.ClientConn,
			invoker grpc.UnaryInvoker, options ...grpc.CallOption,
		) error {
			return invoker(stampCaller(ctx), method, request, reply, cc, options...)
		}),
		grpc.WithStreamInterceptor(func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
			streamer grpc.Streamer, options ...grpc.CallOption,
		) (grpc.ClientStream, error) {
			return streamer(stampCaller(ctx), desc, cc, method, options...)
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	options.ResourceMetadataURL = "https://test-actions.example/.well-known/oauth-protected-resource/mcp"
	if options.Logger == nil {
		options.Logger = slog.New(slog.NewTextHandler(t.Output(), nil))
	}
	handler := NewHandler(pb.NewActionServiceClient(connection), verifyTestBearer, options)
	mux := http.NewServeMux()
	mux.Handle("/mcp", handler)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return &harness{server: server}
}

func (h *harness) url() string { return h.server.URL + "/mcp" }

type bearerTransport struct {
	token string
}

func (b bearerTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	request = request.Clone(request.Context())
	request.Header.Set("Authorization", "Bearer "+b.token)
	return http.DefaultTransport.RoundTrip(request)
}

// connect is an MCP client session as the caller holding token.
func (h *harness) connect(t *testing.T, token string) *mcp.ClientSession {
	t.Helper()
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "v0.0.0"}, nil)
	session, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{
		Endpoint:   h.url(),
		HTTPClient: &http.Client{Transport: bearerTransport{token: token}},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session
}

func callTool(t *testing.T, session *mcp.ClientSession, tool string, arguments string) *mcp.CallToolResult {
	t.Helper()
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: tool, Arguments: json.RawMessage(arguments)})
	if err != nil {
		t.Fatalf("%s(%s): %v", tool, arguments, err)
	}
	return result
}

// text is the one text block of a result.
func text(t *testing.T, result *mcp.CallToolResult) string {
	t.Helper()
	if len(result.Content) != 1 {
		t.Fatalf("content = %d blocks, want 1", len(result.Content))
	}
	block, ok := result.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("content = %T, want text", result.Content[0])
	}
	return block.Text
}
