package oauthspike

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
	"golang.org/x/oauth2"
)

const (
	redirectURI     = "https://client.example.test/callback"
	consentURL      = "https://app.example.test/#/connection-enrollments/"
	serviceAccount  = "system:serviceaccount:example-namespace:approved-caller"
	foreignResource = "https://elsewhere.example.test/mcp"
)

// browser follows no redirects, so each hop of the flow is observable.
var browser = &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}

// fixture is the authorization server on a cloned database, an HTTPS host serving the client's metadata document,
// and a resource server that admits bearers through Server.Authenticate.
type fixture struct {
	server          *Server
	issuer          string
	clientID        string
	resource        string
	documentFetches atomic.Int32
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })

	f := &fixture{}
	// Every path serves the document naming clientID, so another path serves a document naming another client.
	documents := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		f.documentFetches.Add(1)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"client_id":                  f.clientID,
			"client_name":                "Metadata document test client",
			"redirect_uris":              []string{redirectURI},
			"grant_types":                []string{"authorization_code", "refresh_token"},
			"response_types":             []string{"code"},
			"token_endpoint_auth_method": "none",
		})
	}))
	t.Cleanup(documents.Close)
	f.clientID = documents.URL + "/oauth/client.json"

	authorizationServer := httptest.NewUnstartedServer(nil)
	resourceServer := httptest.NewUnstartedServer(nil)
	f.issuer = "http://" + authorizationServer.Listener.Addr().String()
	resourceBase := "http://" + resourceServer.Listener.Addr().String()
	f.resource = resourceBase + "/mcp"

	server, handler, err := New(Config{
		Issuer:              f.issuer,
		Resource:            f.resource,
		ConsentURL:          consentURL,
		AllowInsecureIssuer: true,
	}, db, documents.Client())
	if err != nil {
		t.Fatal(err)
	}
	f.server = server
	authorizationServer.Config.Handler = handler
	authorizationServer.Start()
	t.Cleanup(authorizationServer.Close)

	const metadataPath = "/.well-known/oauth-protected-resource/mcp"
	verify := func(ctx context.Context, token string, _ *http.Request) (*auth.TokenInfo, error) {
		grant, err := server.Authenticate(ctx, token, f.resource)
		if errors.Is(err, ErrInvalidToken) {
			return nil, auth.ErrInvalidToken
		}
		if err != nil {
			return nil, err
		}
		return &auth.TokenInfo{UserID: grant.ServiceAccount, Scopes: grant.Scopes, Expiration: grant.ExpiresAt}, nil
	}
	resourceMux := http.NewServeMux()
	resourceMux.Handle(metadataPath, auth.ProtectedResourceMetadataHandler(&oauthex.ProtectedResourceMetadata{
		Resource:             f.resource,
		AuthorizationServers: []string{f.issuer},
	}))
	resourceMux.Handle("/mcp", auth.RequireBearerToken(verify, &auth.RequireBearerTokenOptions{
		ResourceMetadataURL: resourceBase + metadataPath,
	})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, auth.TokenInfoFromContext(r.Context()).UserID)
	})))
	resourceServer.Config.Handler = resourceMux
	resourceServer.Start()
	t.Cleanup(resourceServer.Close)
	return f
}

// location GETs target and returns where its redirect points.
func location(ctx context.Context, target string) (*url.URL, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	response, err := browser.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusFound {
		body, _ := io.ReadAll(response.Body)
		return nil, fmt.Errorf("GET %s: HTTP %d: %s", target, response.StatusCode, body)
	}
	return response.Location()
}

// consent plays the browser and the operator from the authorization URL to the client's redirect: authorize sends
// the browser to the app's consent page, the app reads the stored enrollment and approves it, and the browser
// resumes at the callback, which redirects to the client. It returns the authorization response and the enrollment's
// handle.
func (f *fixture) consent(ctx context.Context, authorizeURL string) (*auth.AuthorizationResult, string, error) {
	page, err := location(ctx, authorizeURL)
	if err != nil {
		return nil, "", err
	}
	handle, ok := strings.CutPrefix(page.String(), consentURL)
	if !ok {
		return nil, "", fmt.Errorf("authorize redirected to %s, not the consent page", page)
	}
	enrollment, err := f.server.Pending(ctx, handle)
	if err != nil {
		return nil, "", err
	}
	if want := (Enrollment{ClientID: f.clientID, RedirectURI: redirectURI, Resource: f.resource}); enrollment != want {
		return nil, "", fmt.Errorf("pending enrollment %+v, want %+v", enrollment, want)
	}
	resume, err := f.server.Approve(ctx, handle, serviceAccount)
	if err != nil {
		return nil, "", err
	}
	callback, err := location(ctx, resume)
	if err != nil {
		return nil, "", err
	}
	response := callback.Query()
	callback.RawQuery = ""
	if callback.String() != redirectURI {
		return nil, "", fmt.Errorf("the callback redirected to %s, not the client's redirect_uri", callback)
	}
	return &auth.AuthorizationResult{Code: response.Get("code"), State: response.Get("state"), Iss: response.Get("iss")}, handle, nil
}

// token posts a token request and returns the status and the decoded answer.
func (f *fixture) token(t *testing.T, form url.Values) (int, map[string]any) {
	t.Helper()
	response, err := http.PostForm(f.issuer+"/"+tokenPath, form)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var answer map[string]any
	if err := json.NewDecoder(response.Body).Decode(&answer); err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, answer
}

// callResource calls the resource server with token as the bearer.
func (f *fixture) callResource(t *testing.T, token string) (int, string) {
	t.Helper()
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, f.resource, nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, string(body)
}

// TestMCPClientAuthorizesWithMetadataDocument drives the go-sdk MCP client's OAuth handler (CIMD, S256 PKCE, RFC 8707
// resource, RFC 9207 iss checked strictly because the metadata advertises it) from the resource server's 401 through
// consent to a bearer the resource server admits, then refreshes with rotation.
func TestMCPClientAuthorizesWithMetadataDocument(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()

	unauthorized, err := http.Get(f.resource)
	if err != nil {
		t.Fatal(err)
	}
	if unauthorized.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthenticated call: HTTP %d, want 401", unauthorized.StatusCode)
	}
	var authorizeQuery url.Values
	var result *auth.AuthorizationResult
	var handle string
	client, err := auth.NewAuthorizationCodeHandler(&auth.AuthorizationCodeHandlerConfig{
		ClientIDMetadataDocumentConfig: &auth.ClientIDMetadataDocumentConfig{URL: f.clientID},
		RedirectURL:                    redirectURI,
		AuthorizationCodeFetcher: func(ctx context.Context, args *auth.AuthorizationArgs) (*auth.AuthorizationResult, error) {
			authorizeURL, err := url.Parse(args.URL)
			if err != nil {
				return nil, err
			}
			authorizeQuery = authorizeURL.Query()
			result, handle, err = f.consent(ctx, args.URL)
			return result, err
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, f.resource, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := client.Authorize(ctx, request, unauthorized); err != nil {
		t.Fatalf("MCP client authorization: %v", err)
	}

	// Nothing advertised a scope, so the client asked for none, and so for no id_token.
	if authorizeQuery.Has("scope") {
		t.Errorf("authorize request scope %q, want none", authorizeQuery.Get("scope"))
	}
	if got := authorizeQuery.Get("resource"); got != f.resource {
		t.Errorf("authorize request resource %q, want %q", got, f.resource)
	}
	if result.Iss != f.issuer {
		t.Errorf("authorization response iss %q, want %q", result.Iss, f.issuer)
	}
	source, err := client.TokenSource(ctx)
	if err != nil {
		t.Fatal(err)
	}
	issued, err := source.Token()
	if err != nil {
		t.Fatal(err)
	}
	if issued.Extra("id_token") != nil {
		t.Error("the code exchange returned an id_token")
	}
	if strings.Contains(issued.AccessToken, ".") || issued.RefreshToken == "" {
		t.Errorf("access token %q is not opaque, or no refresh token was issued", issued.AccessToken)
	}
	if status, body := f.callResource(t, issued.AccessToken); status != http.StatusOK || body != serviceAccount {
		t.Errorf("resource call with the issued token: HTTP %d %q, want 200 %q", status, body, serviceAccount)
	}
	if _, err := f.server.Authenticate(ctx, issued.AccessToken, foreignResource); !errors.Is(err, ErrInvalidToken) {
		t.Errorf("the token at another resource: %v, want %v", err, ErrInvalidToken)
	}

	// Consent and code are one-shot.
	if _, err := f.server.Approve(ctx, handle, serviceAccount); !errors.Is(err, ErrNotPending) {
		t.Errorf("second approval: %v, want %v", err, ErrNotPending)
	}
	if status, answer := f.token(t, url.Values{
		"grant_type":    {string(oidc.GrantTypeCode)},
		"client_id":     {f.clientID},
		"code":          {result.Code},
		"code_verifier": {oauth2.GenerateVerifier()},
		"redirect_uri":  {redirectURI},
	}); status != http.StatusBadRequest || answer["error"] != "invalid_grant" {
		t.Errorf("replayed code: HTTP %d %v, want 400 invalid_grant", status, answer)
	}

	refresh := url.Values{
		"grant_type":    {string(oidc.GrantTypeRefreshToken)},
		"client_id":     {f.clientID},
		"refresh_token": {issued.RefreshToken},
		"resource":      {f.resource},
	}
	foreign := maps.Clone(refresh)
	foreign.Set("resource", foreignResource)
	if status, answer := f.token(t, foreign); status != http.StatusBadRequest || answer["error"] != "invalid_target" {
		t.Errorf("refresh for another resource: HTTP %d %v, want 400 invalid_target", status, answer)
	}
	status, rotated := f.token(t, refresh)
	if status != http.StatusOK {
		t.Fatalf("refresh: HTTP %d %v", status, rotated)
	}
	if _, ok := rotated["id_token"]; ok || rotated["refresh_token"] == issued.RefreshToken {
		t.Errorf("refresh answered %v, want a new refresh token and no id_token", rotated)
	}
	rotatedAccess, _ := rotated["access_token"].(string)
	if status, _ := f.callResource(t, rotatedAccess); status != http.StatusOK {
		t.Errorf("resource call with the refreshed token: HTTP %d", status)
	}
	// The spent refresh token presented again revokes its family, so the refreshed access token stops working.
	if status, answer := f.token(t, refresh); status != http.StatusBadRequest || answer["error"] != "invalid_grant" {
		t.Errorf("reused refresh token: HTTP %d %v, want 400 invalid_grant", status, answer)
	}
	if status, _ := f.callResource(t, rotatedAccess); status != http.StatusUnauthorized {
		t.Errorf("resource call after refresh reuse: HTTP %d, want 401", status)
	}

	// Authorize and each of the five token requests resolved the client; the stored document answered all but the
	// first.
	if fetches := f.documentFetches.Load(); fetches != 1 {
		t.Errorf("metadata document fetched %d times, want 1", fetches)
	}
}

// TestAuthorizeRefusals: op refuses an untrusted client or redirect_uri without redirecting; Server's S256 and
// resource checks redirect their refusal to the client, with RFC 9207's iss.
func TestAuthorizeRefusals(t *testing.T) {
	f := newFixture(t)
	challenge := oauth2.S256ChallengeFromVerifier(oauth2.GenerateVerifier())
	for _, tc := range []struct {
		name string
		edit func(url.Values)
		// redirectedError is the error the client's redirect_uri receives; empty when the refusal is rendered.
		redirectedError string
	}{
		{
			name: "redirect_uri outside the metadata document",
			edit: func(q url.Values) { q.Set("redirect_uri", "https://attacker.example.test/callback") },
		},
		{
			name: "client_id that is not https",
			edit: func(q url.Values) { q.Set("client_id", strings.Replace(f.clientID, "https://", "http://", 1)) },
		},
		{
			name: "metadata document naming another client",
			edit: func(q url.Values) { q.Set("client_id", f.clientID+"-other") },
		},
		{
			name:            "plain PKCE",
			edit:            func(q url.Values) { q.Set("code_challenge_method", string(oidc.CodeChallengeMethodPlain)) },
			redirectedError: "invalid_request",
		},
		{
			name:            "no PKCE",
			edit:            func(q url.Values) { q.Del("code_challenge"); q.Del("code_challenge_method") },
			redirectedError: "invalid_request",
		},
		{
			name:            "another resource",
			edit:            func(q url.Values) { q.Set("resource", foreignResource) },
			redirectedError: "invalid_target",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			query := url.Values{
				"response_type":         {string(oidc.ResponseTypeCode)},
				"client_id":             {f.clientID},
				"redirect_uri":          {redirectURI},
				"state":                 {"refused-state"},
				"code_challenge":        {challenge},
				"code_challenge_method": {string(oidc.CodeChallengeMethodS256)},
				"resource":              {f.resource},
			}
			tc.edit(query)
			response, err := browser.Get(f.issuer + "/" + authorizationPath + "?" + query.Encode())
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if tc.redirectedError == "" {
				if response.StatusCode != http.StatusBadRequest {
					t.Errorf("HTTP %d, want 400 rendered", response.StatusCode)
				}
				return
			}
			target, err := response.Location()
			if err != nil {
				t.Fatalf("HTTP %d, want a redirect to the client: %v", response.StatusCode, err)
			}
			answer := target.Query()
			if answer.Get("error") != tc.redirectedError || answer.Get("state") != "refused-state" || answer.Get("iss") != f.issuer {
				t.Errorf("redirected to %s, want error %s with the state and iss", target, tc.redirectedError)
			}
		})
	}
}

var stockConfig = Config{
	Issuer:              "http://issuer.example.test",
	Resource:            "http://resource.example.test/mcp",
	ConsentURL:          consentURL,
	AllowInsecureIssuer: true,
}

// TestStockTokenResponseSignsIDTokenWithoutOpenID records why Server replaces token creation: op's
// CreateTokenResponse, which its code and refresh grants call, asks storage for a key to sign an id_token for a
// request that has no openid scope.
func TestStockTokenResponseSignsIDTokenWithoutOpenID(t *testing.T) {
	server, _, err := New(stockConfig, nil, http.DefaultClient)
	if err != nil {
		t.Fatal(err)
	}
	client := &cimdClient{document: clientDocument{ClientID: "https://client.example.test/oauth/client.json", RedirectURIs: []string{redirectURI}}}
	request := &authorization{
		id:             "no-openid",
		clientID:       client.GetID(),
		redirectURI:    redirectURI,
		scopes:         []string{scopeActions, oidc.ScopeOfflineAccess},
		serviceAccount: serviceAccount,
	}
	_, err = op.CreateTokenResponse(op.ContextWithIssuer(t.Context(), stockConfig.Issuer), request, client, server.Provider(), false, "", "")
	if !errors.Is(err, errNoSigningKey) {
		t.Fatalf("op's token response without openid: %v, want it to ask for a signing key", err)
	}
}

// TestStockBearerTokenIsAnEncryptedReference records op's own opaque access token: the token ID and subject
// encrypted under the provider's key. Server's random tokens with stored digests need no key.
func TestStockBearerTokenIsAnEncryptedReference(t *testing.T) {
	server, _, err := New(stockConfig, nil, http.DefaultClient)
	if err != nil {
		t.Fatal(err)
	}
	token, err := op.CreateBearerToken("token-id", "subject", server.Provider().Crypto())
	if err != nil {
		t.Fatal(err)
	}
	plain, err := server.Provider().Crypto().Decrypt(token)
	if err != nil || plain != "token-id:subject" {
		t.Fatalf("op's bearer token decrypts to %q (%v), want the token ID and subject", plain, err)
	}
}
