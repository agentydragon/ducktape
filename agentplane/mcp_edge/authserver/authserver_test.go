package authserver

import (
	"context"
	"net/http"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/modelcontextprotocol/go-sdk/oauthex"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	workloadauth "github.com/agentydragon/ducktape/agentplane/workload_auth/auth" // its name, auth, is go-sdk's
)

// connect connects go-sdk's MCP client to /mcp, which authorizes it through the handler.
func (h *harness) connect(t *testing.T, handler auth.OAuthHandler) *mcp.ClientSession {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-client"}, nil).Connect(ctx,
		&mcp.StreamableClientTransport{Endpoint: h.url + resourcePath, OAuthHandler: handler}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { session.Close() })
	return session
}

// wantCaller calls whoami, which must answer the account.
func wantCaller(t *testing.T, what string, session *mcp.ClientSession, account workloadauth.Account) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	result, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "whoami", Arguments: map[string]any{}})
	if err != nil {
		t.Fatalf("%s: %v", what, err)
	}
	if text, _ := result.Content[0].(*mcp.TextContent); result.IsError || text == nil || text.Text != userID(account) {
		t.Errorf("%s: answered %v, want %s", what, result.Content, userID(account))
	}
}

func TestAnMCPClientAuthorizesByItsMetadataDocumentAndCallsAsTheBoundAccount(t *testing.T) {
	h := start(t)
	handler := must(auth.NewAuthorizationCodeHandler(&auth.AuthorizationCodeHandlerConfig{
		ClientIDMetadataDocumentConfig: &auth.ClientIDMetadataDocumentConfig{URL: h.documentClient(t, "/test-client.json", nil)},
		// A native client listens on a port of its choosing; its document registers the loopback URI without one.
		RedirectURL:              "http://127.0.0.1:49152/test-callback",
		AuthorizationCodeFetcher: h.fetcher(),
		RequestRefreshToken:      true,
	}))(t)
	session := h.connect(t, handler)
	wantCaller(t, "a call after authorizing", session, callerAccount)

	c := h.only(t)
	if _, err := h.connections.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: c.GetVersion(),
		ServiceAccount: &sandboxes.ServiceAccount{Namespace: otherAccount.Namespace, Name: otherAccount.Name},
	}); err != nil {
		t.Fatal(err)
	}
	wantCaller(t, "a call after the rebind", session, otherAccount)
	token := must(must(handler.TokenSource(t.Context()))(t).Token())(t)
	if token.RefreshToken == "" {
		t.Errorf("the client holds no refresh token")
	}
}

func TestAnMCPClientRegistersDynamicallyAndAuthorizes(t *testing.T) {
	h := start(t)
	handler := must(auth.NewAuthorizationCodeHandler(&auth.AuthorizationCodeHandlerConfig{
		DynamicClientRegistrationConfig: &auth.DynamicClientRegistrationConfig{
			Metadata: &oauthex.ClientRegistrationMetadata{
				RedirectURIs: []string{testRedirectURI}, ClientName: "Test DCR client",
				TokenEndpointAuthMethod: "client_secret_basic",
			},
		},
		AuthorizationCodeFetcher: h.fetcher(),
	}))(t)
	session := h.connect(t, handler)
	wantCaller(t, "a call after authorizing", session, callerAccount)
	// The client registered public: it never had a secret to send.
	var method string
	if err := h.db.QueryRowContext(t.Context(),
		`SELECT metadata->>'token_endpoint_auth_method' FROM oauth_registered_client`).Scan(&method); err != nil {
		t.Fatal(err)
	}
	if method != "none" {
		t.Errorf("registered with token_endpoint_auth_method %q", method)
	}
}

func TestTheIssuerIsAnHTTPSOriginOrLoopback(t *testing.T) {
	config := Config{
		UpstreamIssuer: newFakeUpstream(t).server.URL, UpstreamClientID: testUpstreamClient, DocumentClient: http.DefaultClient,
	}
	for issuer, valid := range map[string]bool{
		"https://test-actions.example":      true,
		"http://127.0.0.1:8080":             true,
		"http://test-actions.example":       false,
		"https://":                          false,
		"https://test-actions.example#test": false,
		"https://test-actions.example/mcp":  false,
		"https://test-actions.example?x=1":  false,
		"https://user@test-actions.example": false,
	} {
		config.Issuer = issuer
		if _, err := New(t.Context(), config, nil, nil); (err == nil) != valid {
			t.Errorf("issuer %q: %v", issuer, err)
		}
	}
}
