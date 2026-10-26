package federation

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	testClientID = "login-client"
	testSubject  = "operator-123"
)

type testProvider struct {
	t               *testing.T
	server          *httptest.Server
	issuer          string
	login           *rsa.PrivateKey
	target          *rsa.PrivateKey
	jwksMu          sync.RWMutex
	loginJWKS       []jose.JSONWebKey
	refreshedTokens int
	exchangeStatus  int
	exchangeSubject string
	refreshRefused  bool
	lastForm        url.Values
}

func newTestProvider(t *testing.T) *testProvider {
	t.Helper()
	loginKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	targetKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	provider := &testProvider{t: t, login: loginKey, target: targetKey, exchangeSubject: testSubject}
	provider.loginJWKS = []jose.JSONWebKey{publicJWK(&loginKey.PublicKey, "login-kid")}
	provider.server = httptest.NewServer(http.HandlerFunc(provider.serveHTTP))
	provider.issuer = provider.server.URL
	t.Cleanup(provider.server.Close)
	return provider
}

func (p *testProvider) serveHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/.well-known/openid-configuration":
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer": p.issuer, "jwks_uri": p.issuer + "/jwks/login", "token_endpoint": p.issuer + "/token",
		})
	case "/jwks/login":
		p.jwksMu.RLock()
		keys := append([]jose.JSONWebKey(nil), p.loginJWKS...)
		p.jwksMu.RUnlock()
		_ = json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: keys})
	case "/jwks/target":
		_ = json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{publicJWK(&p.target.PublicKey, "target-kid")}})
	case "/token":
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad form", http.StatusBadRequest)
			return
		}
		p.lastForm = r.Form
		switch r.Form.Get("grant_type") {
		case "refresh_token":
			if p.refreshRefused {
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
				return
			}
			p.refreshedTokens++
			nextAccess := p.sign(p.login, "login-kid", claims(p.issuer, testClientID, testSubject, true, time.Now().Add(10*time.Minute)))
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": nextAccess, "token_type": "Bearer", "expires_in": 600,
				"refresh_token": "rotated-refresh-token",
			})
		case "client_credentials":
			if p.exchangeStatus != 0 {
				w.WriteHeader(p.exchangeStatus)
				_, _ = w.Write([]byte("provider response must not escape"))
				return
			}
			access := p.sign(p.target, "target-kid", claims(p.issuer, "action-audience", p.exchangeSubject, true, time.Now().Add(5*time.Minute)))
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": access, "token_type": "Bearer"})
		default:
			http.Error(w, "unexpected grant", http.StatusBadRequest)
		}
	default:
		http.NotFound(w, r)
	}
}

func publicJWK(key *rsa.PublicKey, kid string) jose.JSONWebKey {
	return jose.JSONWebKey{Key: key, KeyID: kid, Algorithm: string(jose.RS256), Use: "sig"}
}

func claims(issuer, audience, subject string, azp bool, expiry time.Time) map[string]any {
	values := map[string]any{
		"iss": issuer, "aud": audience, "sub": subject,
		"iat": time.Now().Unix(), "exp": expiry.Unix(),
	}
	if azp {
		values["azp"] = audience
	}
	return values
}

func (p *testProvider) sign(key *rsa.PrivateKey, kid string, payload any) string {
	p.t.Helper()
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key},
		(&jose.SignerOptions{}).WithHeader(jose.HeaderKey("kid"), kid))
	if err != nil {
		p.t.Fatal(err)
	}
	signed, err := jwt.Signed(signer).Claims(payload).Serialize()
	if err != nil {
		p.t.Fatal(err)
	}
	return signed
}

func (p *testProvider) config(mode, loginProfile, targetProfile string) Config {
	config := Config{
		Mode: mode, ServiceURL: "http://actions.test", LoginJWKSURI: p.issuer + "/jwks/login",
		LoginTokenProfile: loginProfile,
		Target: ProviderConfig{
			Issuer: p.issuer, Audience: "action-audience", JWKSURI: p.issuer + "/jwks/target", TokenProfile: targetProfile,
		},
		Scope: "openid action", HTTPClient: p.server.Client(),
	}
	if mode == modeDirect {
		config.Target.Audience = testClientID
		config.Target.JWKSURI = p.issuer + "/jwks/login"
	}
	if mode == modeExchange {
		config.TokenEndpoint = p.issuer + "/token"
	}
	return config
}

func (p *testProvider) loginOIDC(renewBefore int) auth.OIDCConfig {
	return auth.OIDCConfig{
		Issuer: p.issuer, ClientID: testClientID, ClientSecret: "test-client-secret",
		SessionSecret: "test-session-secret", PublicBaseURL: "http://app.test",
		TokenRenewBeforeSeconds: renewBefore,
	}
}

type memoryRow struct {
	mu            sync.Mutex
	login         *auth.OperatorSession
	loginBarrier  chan struct{}
	loginCalls    int
	lockedUpdates int
}

func (r *memoryRow) Login(ctx context.Context) (*auth.OperatorSession, error) {
	r.mu.Lock()
	r.loginCalls++
	snapshot := cloneSession(r.login)
	if r.loginBarrier != nil && r.loginCalls == 2 {
		close(r.loginBarrier)
	}
	barrier := r.loginBarrier
	r.mu.Unlock()
	if barrier != nil {
		select {
		case <-barrier:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return snapshot, nil
}

func (r *memoryRow) WithLockedLogin(ctx context.Context, update func(*auth.OperatorSession) (*auth.OperatorSession, error)) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	r.lockedUpdates++
	updated, err := update(cloneSession(r.login))
	if err == nil {
		r.login = cloneSession(updated)
	}
	return err
}

func cloneSession(session *auth.OperatorSession) *auth.OperatorSession {
	if session == nil {
		return nil
	}
	copy := *session
	if session.Tokens != nil {
		tokens := *session.Tokens
		copy.Tokens = &tokens
	}
	return &copy
}

func loginRow(issuer, token string, expiry time.Time, refresh string) *memoryRow {
	return &memoryRow{login: &auth.OperatorSession{
		Issuer: issuer, Subject: testSubject, Username: "operator",
		Tokens: &auth.LoginTokens{AccessToken: token, ExpiresAt: expiry, RefreshToken: refresh},
	}}
}

func newClient(t *testing.T, provider *testProvider, cfg Config, login auth.OIDCConfig) *Client {
	t.Helper()
	// The discovered issuer is the server URL; callers fill that after the server starts.
	login.Issuer = provider.issuer
	cfg.Target.Issuer = provider.issuer
	client, err := New(cfg, login)
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	return client
}

func TestDirectDexNeedsBothExplicitProfiles(t *testing.T) {
	provider := newTestProvider(t)
	loginToken := provider.sign(provider.login, "login-kid", claims(provider.issuer, testClientID, testSubject, false, time.Now().Add(time.Minute)))
	row := loginRow(provider.issuer, loginToken, time.Now().Add(time.Minute), "")
	login := provider.loginOIDC(30)
	login.Issuer = provider.issuer

	accepted := newClient(t, provider, provider.config(modeDirect, profileDex, profileDex), login)
	got, err := accepted.Exchange(context.Background(), row)
	if err != nil || got != loginToken {
		t.Fatalf("Dex direct exchange = (%q, %v), want login token", got, err)
	}

	rejected := newClient(t, provider, provider.config(modeDirect, profileAuthentik, profileDex), login)
	_, err = rejected.Exchange(context.Background(), row)
	assertFederationError(t, err, "operator_federation_token_invalid", http.StatusForbidden)
}

func TestNewDefersLoginProviderDiscoveryUntilRefresh(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		http.Error(w, "provider unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	login := auth.OIDCConfig{
		Issuer: server.URL, ClientID: testClientID, ClientSecret: "secret",
		SessionSecret: "session-secret", PublicBaseURL: "http://agentplane.test",
	}
	cfg := Config{
		Mode: modeDirect, ServiceURL: "http://actions.test", LoginJWKSURI: server.URL + "/jwks/login",
		LoginTokenProfile: profileAuthentik,
		Target: ProviderConfig{
			Issuer: server.URL, Audience: testClientID, JWKSURI: server.URL + "/jwks/target", TokenProfile: profileAuthentik,
		},
		Scope: "openid action", HTTPClient: server.Client(),
	}
	client, err := New(cfg, login)
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if got := requests.Load(); got != 0 {
		t.Fatalf("OIDC discovery requests during construction = %d, want 0", got)
	}

	_, err = client.loginTokenURL(context.Background())
	assertFederationError(t, err, "operator_federation_exchange_failed", http.StatusBadGateway)
	if got := requests.Load(); got != 1 {
		t.Fatalf("OIDC discovery requests after refresh setup = %d, want 1", got)
	}
}

func TestExchangePostsAssertionAndVerifiesTargetIdentity(t *testing.T) {
	provider := newTestProvider(t)
	loginToken := provider.sign(provider.login, "login-kid", claims(provider.issuer, testClientID, testSubject, true, time.Now().Add(time.Minute)))
	row := loginRow(provider.issuer, loginToken, time.Now().Add(time.Minute), "")
	client := newClient(t, provider, provider.config(modeExchange, profileAuthentik, profileAuthentik), provider.loginOIDC(30))

	targetToken, err := client.Exchange(context.Background(), row)
	if err != nil {
		t.Fatalf("Exchange() error = %v", err)
	}
	if targetToken == loginToken {
		t.Fatal("exchange mode returned the login token")
	}
	if provider.lastForm.Get("grant_type") != "client_credentials" ||
		provider.lastForm.Get("client_id") != "action-audience" ||
		provider.lastForm.Get("client_assertion_type") != clientAssertionType ||
		provider.lastForm.Get("client_assertion") != loginToken || provider.lastForm.Get("scope") != "openid action" {
		t.Fatalf("wrong token exchange form: %#v", provider.lastForm)
	}

	provider.exchangeSubject = "another-operator"
	_, err = client.Exchange(context.Background(), row)
	assertFederationError(t, err, "operator_federation_identity_mismatch", http.StatusForbidden)
}

func TestRefreshIsSerializedAndRotatedTokenIsPersisted(t *testing.T) {
	provider := newTestProvider(t)
	oldToken := provider.sign(provider.login, "login-kid", claims(provider.issuer, testClientID, testSubject, true, time.Now().Add(-time.Minute)))
	row := loginRow(provider.issuer, oldToken, time.Now().Add(-time.Minute), "spent-refresh-token")
	row.loginBarrier = make(chan struct{})
	client := newClient(t, provider, provider.config(modeDirect, profileAuthentik, profileAuthentik), provider.loginOIDC(30))

	results := make([]string, 2)
	errs := make([]error, 2)
	var wait sync.WaitGroup
	for i := range results {
		wait.Add(1)
		go func(i int) {
			defer wait.Done()
			results[i], errs[i] = client.Exchange(context.Background(), row)
		}(i)
	}
	wait.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("Exchange[%d] error = %v", i, err)
		}
	}
	if results[0] == "" || results[0] != results[1] {
		t.Fatalf("concurrent exchanges returned different login tokens: %q %q", results[0], results[1])
	}
	if provider.refreshedTokens != 1 {
		t.Fatalf("refresh token used %d times, want once", provider.refreshedTokens)
	}
	row.mu.Lock()
	defer row.mu.Unlock()
	if row.login.Tokens.RefreshToken != "rotated-refresh-token" || row.login.Tokens.AccessToken != results[0] {
		t.Fatalf("rotated login not saved: %#v", row.login.Tokens)
	}
	if row.lockedUpdates != 2 {
		t.Fatalf("WithLockedLogin called %d times, want both replicas to serialize", row.lockedUpdates)
	}
}

func TestRefusedRefreshEndsSession(t *testing.T) {
	provider := newTestProvider(t)
	provider.refreshRefused = true
	oldToken := provider.sign(provider.login, "login-kid", claims(provider.issuer, testClientID, testSubject, true, time.Now().Add(-time.Minute)))
	row := loginRow(provider.issuer, oldToken, time.Now().Add(-time.Minute), "spent-refresh-token")
	client := newClient(t, provider, provider.config(modeDirect, profileAuthentik, profileAuthentik), provider.loginOIDC(30))

	_, err := client.Exchange(context.Background(), row)
	assertFederationError(t, err, "operator_reauthentication_required", http.StatusUnauthorized)
	row.mu.Lock()
	defer row.mu.Unlock()
	if row.login != nil {
		t.Fatalf("refused refresh retained login: %#v", row.login)
	}
}

func TestRefreshTokenResponseAcceptsAbsoluteExpiry(t *testing.T) {
	now := time.Date(2026, time.October, 23, 12, 0, 0, 0, time.UTC)
	got, err := parseTokenResponse([]byte(`{"access_token":"refreshed","expires_at":"2026-10-23T12:05:00Z"}`), now)
	if err != nil {
		t.Fatalf("parseTokenResponse() error = %v", err)
	}
	want := now.Add(5 * time.Minute)
	if !got.ExpiresAt.Equal(want) {
		t.Fatalf("expiry = %s, want %s", got.ExpiresAt, want)
	}
}

func TestJWKSUnknownKeyRefreshAndSafeUpstreamError(t *testing.T) {
	provider := newTestProvider(t)
	secondKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := newTokenVerifier(ProviderConfig{
		Issuer: provider.issuer, Audience: testClientID, JWKSURI: provider.issuer + "/jwks/login", TokenProfile: profileAuthentik,
	}, provider.server.Client())
	if err != nil {
		t.Fatal(err)
	}
	first := provider.sign(provider.login, "login-kid", claims(provider.issuer, testClientID, testSubject, true, time.Now().Add(time.Minute)))
	if _, err := verifier.verify(context.Background(), first, time.Now()); err != nil {
		t.Fatalf("verify first key: %v", err)
	}
	provider.jwksMu.Lock()
	provider.loginJWKS = []jose.JSONWebKey{publicJWK(&secondKey.PublicKey, "rotated-kid")}
	provider.jwksMu.Unlock()
	rotated := provider.sign(secondKey, "rotated-kid", claims(provider.issuer, testClientID, testSubject, true, time.Now().Add(time.Minute)))
	if _, err := verifier.verify(context.Background(), rotated, time.Now()); err != nil {
		t.Fatalf("verify key after unknown-kid refresh: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "body must not leak", http.StatusServiceUnavailable)
	}))
	defer server.Close()
	failedVerifier, err := newTokenVerifier(ProviderConfig{
		Issuer: provider.issuer, Audience: testClientID, JWKSURI: server.URL + "/keys?secret=hidden", TokenProfile: profileAuthentik,
	}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	_, err = failedVerifier.verify(context.Background(), first, time.Now())
	var upstream *UpstreamError
	if !errors.As(err, &upstream) {
		t.Fatalf("verify error = %T %v, want UpstreamError", err, err)
	}
	if strings.Contains(upstream.URL, "secret") || strings.Contains(err.Error(), "body must not leak") || upstream.UpstreamStatus != http.StatusServiceUnavailable {
		t.Fatalf("unsafe upstream detail: %#v, %v", upstream, err)
	}
}

func assertFederationError(t *testing.T, err error, code string, status int) {
	t.Helper()
	var federationErr *FederationError
	if !errors.As(err, &federationErr) {
		t.Fatalf("error = %T %v, want FederationError", err, err)
	}
	if federationErr.Code != code || federationErr.Status != status || err.Error() != code {
		t.Fatalf("FederationError = %#v, want code %q status %d", federationErr, code, status)
	}
}
