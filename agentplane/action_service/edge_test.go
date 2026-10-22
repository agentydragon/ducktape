package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type edgeBearerTransport struct{ token string }

func (b edgeBearerTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	r.Header.Set("Authorization", "Bearer "+b.token)
	return http.DefaultTransport.RoundTrip(r)
}

func TestTheListenerServesOAuthAndRoutesMCPAsTheConnectionsCurrentCaller(t *testing.T) {
	// The protocol package tests exercise the authorization exchange; this test exercises its assembly
	// with the actual ActionService, policy informer, database, and MCP tools on one listener.
	var issuer string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/.well-known/openid-configuration" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
			"jwks_uri": issuer + "/jwks", "id_token_signing_alg_values_supported": []string{"RS256"},
		})
	}))
	issuer = upstream.URL
	defer upstream.Close()
	secret := filepath.Join(t.TempDir(), "client-secret")
	if err := os.WriteFile(secret, []byte("test-secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	account := auth.Account{Namespace: policyNamespace, Name: "test-external-mcp"}
	rebound := auth.Account{Namespace: policyNamespace, Name: "test-external-rebound"}
	setCallerLabel(t, account, true)
	setCallerLabel(t, rebound, true)
	const edgeIssuer = "https://test-actions.example"
	var databaseURL string
	service := startConfigured(t, kube.Config, &catalog.Catalog{}, func(c *config, _ *deps) {
		databaseURL = c.DatabaseURL
		c.OAuth = &oauthSettings{
			BaseURL: edgeIssuer, IntegrationAppURL: "https://test-app.example", UpstreamIssuer: issuer,
			UpstreamClientID: "test-client", UpstreamClientSecretFile: secret, UpstreamSubject: "test-upstream-subject",
		}
		c.OAuth.ApprovingOperator.Issuer = "https://test-operator.example"
		c.OAuth.ApprovingOperator.Subject = "test-operator"
	})
	waitReady(t, service)
	for _, path := range []string{"/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"} {
		response, err := http.Get(service.url + path)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusOK {
			t.Fatalf("%s: %d", path, response.StatusCode)
		}
	}
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	token, connectionID, grantID := "test-external-bearer", uuid.New(), uuid.New()
	bound := []byte(fmt.Sprintf(`{"namespace":%q,"name":%q}`, account.Namespace, account.Name))
	queries := []struct {
		query string
		args  []any
	}{
		{`INSERT INTO external_connection (id, display_name, version, created_at, updated_at, bound_caller) VALUES ($1, 'test', 1, now(), now(), $2)`, []any{connectionID, string(bound)}},
		{`INSERT INTO external_connection_grant (id, connection_id, revision, issuer, client_id, status, created_at, activated_at, original_caller) VALUES ($1, $2, 1, $3, 'test-client', 'active', now(), now(), $4)`, []any{grantID, connectionID, edgeIssuer, string(bound)}},
		{`INSERT INTO connection_enrollment (id, handle_hash, issuer, client_id, redirect_uri, code_challenge, upstream_url, expires_at, version) VALUES ($1, 'test-handle', $2, 'test-client', 'http://127.0.0.1/callback', 'test-challenge', 'https://test-upstream.example', now()+interval '1 hour', 1)`, []any{grantID, edgeIssuer}},
		{`INSERT INTO oauth_authorization (id, client_id, redirect_uri, code_challenge, expires_at, upstream_state_hash, upstream_verifier) VALUES ($1, 'test-client', 'http://127.0.0.1/callback', 'test-challenge', now()+interval '1 hour', 'test-state', 'test-verifier')`, []any{grantID}},
		{`INSERT INTO oauth_token (token_hash, authorization_id, kind, expires_at) VALUES ($1, $2, 'access', now()+interval '1 hour')`, []any{fmt.Sprintf("%x", sha256.Sum256([]byte(token))), grantID}},
	}
	for _, query := range queries {
		if _, err := db.ExecContext(t.Context(), query.query, query.args...); err != nil {
			t.Fatal(err)
		}
	}
	own, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: account.Namespace, Name: account.Name}},
		store.ActionRequestInput{IdempotencyKey: "test-external", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"}, Arguments: json.RawMessage(`{}`), Title: "test external request"}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test-external-client"}, nil)
	session, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{
		Endpoint: service.url + "/mcp", HTTPClient: &http.Client{Transport: edgeBearerTransport{token: token}},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	read := func() *mcp.CallToolResult {
		result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "get_action_request", Arguments: map[string]any{"request_id": own.ID.String(), "include_fields": []string{"id"}, "wait": map[string]any{"wait_seconds": 0.1, "wait_until": "decision"}}})
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	if result := read(); result.IsError {
		t.Fatalf("own receipt: %+v", result)
	}
	newBound := fmt.Sprintf(`{"namespace":%q,"name":%q}`, rebound.Namespace, rebound.Name)
	if _, err := db.ExecContext(t.Context(), `UPDATE external_connection SET bound_caller=$1 WHERE id=$2`, newBound, connectionID); err != nil {
		t.Fatal(err)
	}
	if result := read(); !result.IsError {
		t.Fatal("a rebound Connection read its previous account's receipt")
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE external_connection_grant SET status='revoked', revoked_at=now() WHERE id=$1`, grantID); err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, service.url+"/mcp", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("revoked bearer: %d", response.StatusCode)
	}
}
