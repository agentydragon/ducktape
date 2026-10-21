// Package oauthprovider fakes an MCP server's OAuth authorization server for tests of the MCP linkage. Its token
// endpoint grants every authorization code but RefusedCode, and answers each refresh as the test sets.
package oauthprovider

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
)

const (
	// ClientID is the client Server registers.
	ClientID = "test-client"
	// RedirectURI is where Server's flows return.
	RedirectURI = "https://test-actions.example/mcp-linkage/callback"
	// RefusedCode is the authorization code the token endpoint refuses as GitHub does: 200 with an OAuth error.
	RefusedCode = "test-refused-code"
	// RefusedError is the OAuth error RefusedCode gets.
	RefusedError = "bad_verification_code"
)

// Options are what a Provider publishes and grants.
type Options struct {
	// Metadata publishes the MCP server's protected resource metadata and the authorization server's metadata; without
	// it every metadata URL answers 404, so a linkage uses the endpoints Server configures.
	Metadata bool
	// CIMD is whether the authorization server's metadata advertises Client ID Metadata Document support.
	CIMD bool
	// ExpiresIn is the lifetime in seconds of each access token granted; 0 grants tokens without an expiry.
	ExpiresIn int
	// RefreshTokens is whether each grant carries a refresh token.
	RefreshTokens bool
}

// Provider is the fake, which serves until the test ends.
type Provider struct {
	// URL is the base URL. The MCP server is at URL/mcp, the authorization server's endpoints at URL/authorize and
	// URL/token.
	URL     string
	options Options

	mu sync.Mutex
	// requests are the token endpoint's requests, in order.
	requests []url.Values
	// grants counts the tokens granted, which numbers them.
	grants int
	// refresh answers a refresh; nil grants one.
	refresh *answer
	// held blocks refreshes while it is open.
	held chan struct{}
}

type answer struct {
	status int
	body   any
}

// Scopes are those the metadata advertises.
var Scopes = []string{"test-read", "test-write"}

func New(t *testing.T, options Options) *Provider {
	t.Helper()
	p := &Provider{options: options}
	server := httptest.NewServer(http.HandlerFunc(p.serve))
	t.Cleanup(server.Close)
	p.URL = server.URL
	return p
}

// Server is an MCP server at the provider, with its endpoints configured and a registered client.
func (p *Provider) Server(serverID string) linkage.Server {
	return linkage.Server{
		ServerID: serverID, ServerURL: p.URL + "/mcp", AuthorizationEndpoint: new(p.URL + "/authorize"),
		TokenEndpoint: new(p.URL + "/token"), ClientID: new(ClientID), RedirectURI: RedirectURI,
	}
}

// Refuse answers each refresh from now on with status and the body as JSON.
func (p *Provider) Refuse(status int, body any) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.refresh = &answer{status: status, body: body}
}

// Hold blocks each refresh until the returned release is called.
func (p *Provider) Hold() (release func()) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.held = make(chan struct{})
	return sync.OnceFunc(func() { close(p.held) })
}

// Requests are the token endpoint's requests of grant_type grantType, in order.
func (p *Provider) Requests(grantType string) []url.Values {
	p.mu.Lock()
	defer p.mu.Unlock()
	var found []url.Values
	for _, request := range p.requests {
		if request.Get("grant_type") == grantType {
			found = append(found, request)
		}
	}
	return found
}

func (p *Provider) serve(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.URL.Path == "/token" && r.Method == http.MethodPost:
		p.serveToken(w, r)
	case !p.options.Metadata:
		http.NotFound(w, r)
	case r.URL.Path == "/mcp":
		w.Header().Set("WWW-Authenticate", fmt.Sprintf(`Bearer resource_metadata=%q`, p.URL+"/.well-known/oauth-protected-resource/mcp"))
		w.WriteHeader(http.StatusUnauthorized)
	case r.URL.Path == "/.well-known/oauth-protected-resource/mcp":
		writeJSON(w, http.StatusOK, map[string]any{
			"resource": p.URL + "/mcp", "authorization_servers": []string{p.URL}, "scopes_supported": Scopes,
		})
	case r.URL.Path == "/.well-known/oauth-authorization-server":
		writeJSON(w, http.StatusOK, map[string]any{
			"issuer": p.URL, "authorization_endpoint": p.URL + "/authorize", "token_endpoint": p.URL + "/token",
			"response_types_supported": []string{"code"}, "code_challenge_methods_supported": []string{"S256"},
			"client_id_metadata_document_supported": p.options.CIMD,
		})
	default:
		http.NotFound(w, r)
	}
}

func (p *Provider) serveToken(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	p.mu.Lock()
	p.requests = append(p.requests, r.PostForm)
	refresh, held := p.refresh, p.held
	p.mu.Unlock()
	switch r.PostForm.Get("grant_type") {
	case "authorization_code":
		if r.PostForm.Get("code") == RefusedCode {
			writeJSON(w, http.StatusOK, map[string]string{"error": RefusedError})
			return
		}
		writeJSON(w, http.StatusOK, p.grant("test-access-token"))
	case "refresh_token":
		if held != nil {
			<-held
		}
		if refresh != nil {
			writeJSON(w, refresh.status, refresh.body)
			return
		}
		writeJSON(w, http.StatusOK, p.grant("test-refreshed-token"))
	default:
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unsupported_grant_type"})
	}
}

// grant is a new token, numbered after prefix.
func (p *Provider) grant(prefix string) map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.grants++
	token := map[string]any{"access_token": fmt.Sprintf("%s-%d", prefix, p.grants), "token_type": "Bearer"}
	if p.options.ExpiresIn != 0 {
		token["expires_in"] = p.options.ExpiresIn
	}
	if p.options.RefreshTokens {
		token["refresh_token"] = fmt.Sprintf("test-refresh-token-%d", p.grants)
	}
	return token
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// State is the state the authorization URL of a started flow carries, which its callback presents.
func State(t *testing.T, authorizationURL string) string {
	t.Helper()
	parsed, err := url.Parse(authorizationURL)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Query().Get("state")
}
