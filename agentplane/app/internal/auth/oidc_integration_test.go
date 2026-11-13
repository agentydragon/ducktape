package auth

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	oidcTestClientID     = "agentplane-test-client"
	oidcTestClientSecret = "agentplane-test-client-secret"
	oidcTestSessionKey   = "agentplane-test-session-key"

	oidcFailureIssuer    = "issuer"
	oidcFailureAudience  = "audience"
	oidcFailureParty     = "azp"
	oidcFailureExpired   = "expired"
	oidcFailureSubject   = "subject"
	oidcFailureNonce     = "nonce"
	oidcFailureSignature = "signature"
)

type oidcTestIdentity struct {
	subject  string
	username string
}

type oidcAuthorization struct {
	identity    oidcTestIdentity
	nonce       string
	challenge   string
	redirectURI string
	failure     string
	tokenStatus int
}

type signedOIDCProvider struct {
	t         *testing.T
	server    *httptest.Server
	client    *http.Client
	issuer    string
	signing   *rsa.PrivateKey
	wrongSign *rsa.PrivateKey

	mu                  sync.Mutex
	nextIdentity        oidcTestIdentity
	nextFailure         string
	nextTokenStatus     int
	lastAuthorization   url.Values
	lastVerifier        string
	tokenRequests       int
	authorizations      map[string]oidcAuthorization
	exchangeBarrier     int
	exchangeCount       int
	exchangesAllArrived chan struct{}
}

type oidcSessionPolicy struct {
	maxSeconds  int
	idleSeconds int
	stepSeconds int
}

type oidcHTTPResponse struct {
	status  int
	header  http.Header
	cookies []*http.Cookie
	body    string
}

type oidcIntegration struct {
	server   *httptest.Server
	provider *signedOIDCProvider
	auths    []*Auth
	dbs      []*sql.DB
	appURL   string
}

func newSignedOIDCProvider(t *testing.T) *signedOIDCProvider {
	t.Helper()
	signing, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	wrongSigning, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	provider := &signedOIDCProvider{
		t: t, signing: signing, wrongSign: wrongSigning,
		nextIdentity:   oidcTestIdentity{subject: "oidc-subject", username: "test-operator"},
		authorizations: make(map[string]oidcAuthorization),
	}
	provider.server = httptest.NewTLSServer(http.HandlerFunc(provider.serveHTTP))
	provider.issuer = provider.server.URL
	provider.client = provider.server.Client()
	provider.client.Timeout = 20 * time.Second
	provider.client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	t.Cleanup(provider.server.Close)
	return provider
}

func (p *signedOIDCProvider) serveHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/.well-known/openid-configuration":
		writeOIDCTestJSON(w, http.StatusOK, map[string]any{
			"issuer": p.issuer, "authorization_endpoint": p.issuer + "/authorize",
			"token_endpoint": p.issuer + "/token", "jwks_uri": p.issuer + "/jwks",
			"response_types_supported": []string{"code"}, "subject_types_supported": []string{"public"},
			"id_token_signing_alg_values_supported": []string{"RS256"},
		})
	case "/jwks":
		writeOIDCTestJSON(w, http.StatusOK, jose.JSONWebKeySet{Keys: []jose.JSONWebKey{oidcPublicJWK(&p.signing.PublicKey)}})
	case "/authorize":
		p.authorize(w, r)
	case "/token":
		p.exchange(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (p *signedOIDCProvider) authorize(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	if query.Get("response_type") != "code" || query.Get("client_id") != oidcTestClientID ||
		query.Get("state") == "" || query.Get("nonce") == "" || query.Get("code_challenge_method") != "S256" ||
		query.Get("code_challenge") == "" || query.Get("redirect_uri") == "" {
		http.Error(w, "invalid authorization request", http.StatusBadRequest)
		return
	}
	p.mu.Lock()
	p.lastAuthorization = query
	code := fmt.Sprintf("authorization-code-%d", len(p.authorizations)+1)
	p.authorizations[code] = oidcAuthorization{
		identity: p.nextIdentity, nonce: query.Get("nonce"), challenge: query.Get("code_challenge"),
		redirectURI: query.Get("redirect_uri"), failure: p.nextFailure, tokenStatus: p.nextTokenStatus,
	}
	p.mu.Unlock()
	callback, err := url.Parse(query.Get("redirect_uri"))
	if err != nil {
		http.Error(w, "invalid redirect URI", http.StatusBadRequest)
		return
	}
	callbackQuery := callback.Query()
	callbackQuery.Set("code", code)
	callbackQuery.Set("state", query.Get("state"))
	callback.RawQuery = callbackQuery.Encode()
	http.Redirect(w, r, callback.String(), http.StatusFound)
}

func (p *signedOIDCProvider) exchange(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		http.Error(w, "invalid token request", http.StatusBadRequest)
		return
	}
	code := r.Form.Get("code")
	verifier := r.Form.Get("code_verifier")
	p.mu.Lock()
	authorization, exists := p.authorizations[code]
	p.lastVerifier = verifier
	p.tokenRequests++
	barrier := p.exchangeBarrier
	if barrier > 0 {
		p.exchangeCount++
		if p.exchangeCount == barrier {
			close(p.exchangesAllArrived)
		}
	}
	arrived := p.exchangesAllArrived
	p.mu.Unlock()
	if !exists {
		writeOIDCTestJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
		return
	}
	if authorization.tokenStatus != 0 {
		writeOIDCTestJSON(w, authorization.tokenStatus, map[string]string{
			"error": "provider_refused", "error_description": "test-private-provider-response",
		})
		return
	}
	if r.Form.Get("grant_type") != "authorization_code" || r.Form.Get("redirect_uri") != authorization.redirectURI ||
		!validClientAuthentication(r, oidcTestClientID, oidcTestClientSecret) ||
		!validPKCE(verifier, authorization.challenge) {
		writeOIDCTestJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
		return
	}
	if barrier > 0 {
		select {
		case <-arrived:
		case <-r.Context().Done():
			return
		case <-time.After(15 * time.Second):
			p.t.Errorf("token requests did not reach callback barrier of %d", barrier)
			http.Error(w, "test callback barrier timed out", http.StatusGatewayTimeout)
			return
		}
	}
	idToken, err := p.signIDToken(authorization)
	if err != nil {
		p.t.Errorf("sign test ID token: %v", err)
		http.Error(w, "test token signing failed", http.StatusInternalServerError)
		return
	}
	writeOIDCTestJSON(w, http.StatusOK, map[string]any{
		"access_token": "test-access-token", "refresh_token": "test-refresh-token",
		"token_type": "Bearer", "expires_in": 3600, "id_token": idToken,
	})
}

func (p *signedOIDCProvider) signIDToken(authorization oidcAuthorization) (string, error) {
	claims := map[string]any{
		"iss": p.issuer, "sub": authorization.identity.subject, "aud": oidcTestClientID,
		"azp": oidcTestClientID, "iat": time.Now().Unix(), "exp": time.Now().Add(time.Hour).Unix(),
		"nonce": authorization.nonce, "preferred_username": authorization.identity.username,
	}
	key := p.signing
	switch authorization.failure {
	case oidcFailureIssuer:
		claims["iss"] = "https://wrong-issuer.invalid"
	case oidcFailureAudience:
		claims["aud"] = "wrong-client"
	case oidcFailureParty:
		claims["azp"] = "wrong-client"
	case oidcFailureExpired:
		claims["exp"] = time.Now().Add(-time.Minute).Unix()
	case oidcFailureSubject:
		claims["sub"] = ""
	case oidcFailureNonce:
		claims["nonce"] = "wrong-nonce"
	case oidcFailureSignature:
		key = p.wrongSign
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key},
		(&jose.SignerOptions{}).WithHeader(jose.HeaderKey("kid"), "oidc-test-key"))
	if err != nil {
		return "", err
	}
	return jwt.Signed(signer).Claims(claims).Serialize()
}

func (p *signedOIDCProvider) setNext(identity oidcTestIdentity, failure string, tokenStatus int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.nextIdentity, p.nextFailure, p.nextTokenStatus = identity, failure, tokenStatus
}

func (p *signedOIDCProvider) setExchangeBarrier(count int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.exchangeBarrier = count
	p.exchangeCount = 0
	p.exchangesAllArrived = make(chan struct{})
}

func (p *signedOIDCProvider) authorizationParameters() url.Values {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.lastAuthorization
}

func (p *signedOIDCProvider) verifier() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.lastVerifier
}

func validClientAuthentication(r *http.Request, clientID, clientSecret string) bool {
	id, secret, ok := r.BasicAuth()
	if ok {
		return id == clientID && secret == clientSecret
	}
	return r.Form.Get("client_id") == clientID && r.Form.Get("client_secret") == clientSecret
}

func validPKCE(verifier, challenge string) bool {
	if len(verifier) < 43 || len(verifier) > 128 {
		return false
	}
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:]) == challenge
}

func oidcPublicJWK(key *rsa.PublicKey) jose.JSONWebKey {
	return jose.JSONWebKey{Key: key, KeyID: "oidc-test-key", Algorithm: string(jose.RS256), Use: "sig"}
}

func writeOIDCTestJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func newOIDCIntegration(t *testing.T, replicas int, policy oidcSessionPolicy) *oidcIntegration {
	t.Helper()
	baseStore, dsn := integrationDB(t)
	provider := newSignedOIDCProvider(t)
	var handlers []http.Handler
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		index := 0
		if r.Header.Get("X-Test-Replica") == "replica-b" && len(handlers) > 1 {
			index = 1
		}
		handlers[index].ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)

	if policy.maxSeconds == 0 {
		policy = oidcSessionPolicy{maxSeconds: 7 * 24 * 60 * 60, idleSeconds: 24 * 60 * 60, stepSeconds: 5 * 60}
	}
	dbs := []*sql.DB{baseStore.db}
	auths := make([]*Auth, 0, replicas)
	for index := 0; index < replicas; index++ {
		db := baseStore.db
		if index > 0 {
			var err error
			db, err = sql.Open("postgres", dsn)
			if err != nil {
				t.Fatal(err)
			}
			db.SetMaxOpenConns(8)
			if err := db.PingContext(t.Context()); err != nil {
				t.Fatal(err)
			}
			dbs = append(dbs, db)
			t.Cleanup(func() { _ = db.Close() })
		}
		config := Config{
			OIDC: &OIDCConfig{
				Issuer: provider.issuer, ClientID: oidcTestClientID, ClientSecret: oidcTestClientSecret,
				SessionSecret: oidcTestSessionKey, PublicBaseURL: server.URL,
				SessionMaxSeconds: policy.maxSeconds, SessionIdleSeconds: policy.idleSeconds,
				SessionActivityStepSeconds: policy.stepSeconds,
			},
			Database: db, HTTPClient: provider.client,
		}
		a, err := New(config)
		if err != nil {
			t.Fatal(err)
		}
		mux := http.NewServeMux()
		a.RegisterRoutes(mux)
		protected := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			caller, ok := CallerFromContext(r.Context())
			if !ok {
				writeError(w, http.StatusInternalServerError, "caller missing")
				return
			}
			writeJSON(w, http.StatusOK, map[string]string{"kind": string(caller.Kind), "principal": caller.Principal})
		})
		mux.Handle("GET /protected", a.RequireCaller(protected))
		mux.Handle("POST /protected", a.RequireCaller(protected))
		auths = append(auths, a)
		handlers = append(handlers, a.Middleware(mux))
	}
	return &oidcIntegration{server: server, provider: provider, auths: auths, dbs: dbs, appURL: server.URL}
}

func (f *oidcIntegration) browser(t *testing.T) *http.Client {
	t.Helper()
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	return &http.Client{
		Jar: jar, Timeout: 20 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (f *oidcIntegration) request(t *testing.T, client *http.Client, replica, method, target string, headers map[string]string) oidcHTTPResponse {
	t.Helper()
	request, err := http.NewRequestWithContext(t.Context(), method, target, nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("X-Test-Replica", replica)
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return oidcHTTPResponse{status: response.StatusCode, header: response.Header.Clone(), cookies: response.Cookies(), body: string(body)}
}

func (f *oidcIntegration) beginLogin(t *testing.T, client *http.Client, replica string) (string, *http.Cookie) {
	t.Helper()
	response := f.request(t, client, replica, http.MethodGet, f.appURL+"/auth/login", nil)
	if response.status != http.StatusFound {
		t.Fatalf("login status = %d, want 302: %s", response.status, response.body)
	}
	loginURL := response.header.Get("Location")
	if loginURL == "" {
		t.Fatal("login did not redirect to the identity provider")
	}
	return f.provider.authorizationRedirect(t, loginURL), responseCookie(t, response, f.auths[0].cfg.OIDC.CookieName())
}

func (p *signedOIDCProvider) authorizationRedirect(t *testing.T, authorizationURL string) string {
	t.Helper()
	response, err := p.client.Get(authorizationURL)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusFound {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("provider authorization status = %d, want 302: %s", response.StatusCode, body)
	}
	callback := response.Header.Get("Location")
	if callback == "" {
		t.Fatal("provider authorization response omitted callback URL")
	}
	return callback
}

func responseCookie(t *testing.T, response oidcHTTPResponse, name string) *http.Cookie {
	t.Helper()
	for _, cookie := range response.cookies {
		if cookie.Name == name {
			return cookie
		}
	}
	t.Fatalf("response did not set %s cookie", name)
	return nil
}

func cookieRowID(t *testing.T, a *Auth, cookie *http.Cookie) string {
	t.Helper()
	handle, err := a.signer.unsign(cookie.Value)
	if err != nil {
		t.Fatalf("verify session cookie: %v", err)
	}
	return rowID(handle)
}

func setBrowserCookie(t *testing.T, client *http.Client, appURL string, cookie *http.Cookie) {
	t.Helper()
	base, err := url.Parse(appURL)
	if err != nil {
		t.Fatal(err)
	}
	client.Jar.SetCookies(base, []*http.Cookie{cookie})
}

func changeCallbackState(t *testing.T, callbackURL, state string) string {
	t.Helper()
	parsed, err := url.Parse(callbackURL)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	query.Set("state", state)
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

func assertLoggedIn(t *testing.T, f *oidcIntegration, client *http.Client, replica, username, subject string) {
	t.Helper()
	me := f.request(t, client, replica, http.MethodGet, f.appURL+"/auth/me", nil)
	if me.status != http.StatusOK || !strings.Contains(me.body, `"username":"`+username+`"`) {
		t.Fatalf("/auth/me = (%d, %s), want username %q", me.status, me.body, username)
	}
	protected := f.request(t, client, replica, http.MethodGet, f.appURL+"/protected", nil)
	if protected.status != http.StatusOK || !strings.Contains(protected.body, `"principal":"`+username+`"`) {
		t.Fatalf("protected route = (%d, %s), want operator %q", protected.status, protected.body, username)
	}
	var storedSubject string
	if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT operator_subject FROM operator_browser_session WHERE operator_username=$1`, username).Scan(&storedSubject); err != nil || storedSubject != subject {
		t.Fatalf("stored subject for %q = (%q, %v), want %q", username, storedSubject, err, subject)
	}
}

func TestSignedOIDCLoginUsesPKCEAndRotatesCookie(t *testing.T) {
	f := newOIDCIntegration(t, 1, oidcSessionPolicy{})
	client := f.browser(t)
	callbackURL, pendingCookie := f.beginLogin(t, client, "replica-a")
	query := f.provider.authorizationParameters()
	if query.Get("code_challenge_method") != "S256" || query.Get("code_challenge") == "" || query.Get("nonce") == "" || query.Get("state") == "" {
		t.Fatalf("authorization parameters = %v, want state, nonce, and S256 PKCE", query)
	}
	pendingID := cookieRowID(t, f.auths[0], pendingCookie)
	var payloadText string
	if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT payload::text FROM operator_browser_session WHERE id=$1`, pendingID).Scan(&payloadText); err != nil {
		t.Fatal(err)
	}
	var pending map[string]loginAttempt
	if err := json.Unmarshal([]byte(payloadText), &pending); err != nil {
		t.Fatal(err)
	}
	attempt, exists := pending[query.Get("state")]
	if len(pending) != 1 || !exists || attempt.Nonce != query.Get("nonce") || !validPKCE(attempt.CodeVerifier, query.Get("code_challenge")) {
		t.Fatalf("stored pending login = %+v, want provider-bound nonce and verifier", pending)
	}
	callback := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil)
	if callback.status != http.StatusSeeOther {
		t.Fatalf("valid callback status = %d, want 303: %s", callback.status, callback.body)
	}
	loggedInCookie := responseCookie(t, callback, f.auths[0].cfg.OIDC.CookieName())
	if pendingCookie.Value == loggedInCookie.Value {
		t.Fatal("login callback kept the pending session cookie instead of rotating it")
	}
	if loggedInCookie.MaxAge <= 0 || loggedInCookie.MaxAge > f.auths[0].cfg.OIDC.SessionMaxSeconds {
		t.Fatalf("logged-in cookie MaxAge = %d, want a positive duration bounded by the absolute session lifetime", loggedInCookie.MaxAge)
	}
	if count, err := countSessionRows(t, f.dbs[0], pendingID); err != nil || count != 0 {
		t.Fatalf("pending session row count = (%d, %v), want 0 after callback", count, err)
	}
	loggedInID := cookieRowID(t, f.auths[0], loggedInCookie)
	var issuer, subject, username sql.NullString
	var accessToken, refreshToken sql.NullString
	if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT operator_issuer, operator_subject, operator_username, access_token, refresh_token
		FROM operator_browser_session WHERE id=$1`, loggedInID).Scan(&issuer, &subject, &username, &accessToken, &refreshToken); err != nil {
		t.Fatal(err)
	}
	if issuer.String != f.provider.issuer || subject.String != "oidc-subject" || username.String != "test-operator" || accessToken.Valid || refreshToken.Valid {
		t.Fatalf("stored operator identity/tokens = (%+v, %+v, %+v, %+v, %+v)", issuer, subject, username, accessToken, refreshToken)
	}
	if verifier := f.provider.verifier(); verifier == "" || !validPKCE(verifier, query.Get("code_challenge")) {
		t.Fatalf("provider received code verifier %q that does not match the authorization challenge", verifier)
	}
	assertLoggedIn(t, f, client, "replica-a", "test-operator", "oidc-subject")
	replayed := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil)
	if replayed.status != http.StatusUnauthorized {
		t.Fatalf("replayed callback status = %d, want 401: %s", replayed.status, replayed.body)
	}
	if me := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); me.status != http.StatusOK {
		t.Fatalf("callback replay invalidated the logged-in session: %d %s", me.status, me.body)
	}
	oldBrowser := f.browser(t)
	setBrowserCookie(t, oldBrowser, f.appURL, pendingCookie)
	if me := f.request(t, oldBrowser, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); me.status != http.StatusUnauthorized {
		t.Fatalf("old pending cookie status = %d, want 401", me.status)
	}
}

func TestSignedOIDCRejectsInvalidClaimsAndState(t *testing.T) {
	for _, test := range []struct {
		name    string
		failure string
		state   bool
	}{
		{name: "issuer", failure: oidcFailureIssuer},
		{name: "audience", failure: oidcFailureAudience},
		{name: "azp", failure: oidcFailureParty},
		{name: "signature", failure: oidcFailureSignature},
		{name: "expired ID token", failure: oidcFailureExpired},
		{name: "empty subject", failure: oidcFailureSubject},
		{name: "nonce", failure: oidcFailureNonce},
		{name: "state", state: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newOIDCIntegration(t, 1, oidcSessionPolicy{})
			f.provider.setNext(oidcTestIdentity{subject: "oidc-subject", username: "test-operator"}, test.failure, 0)
			client := f.browser(t)
			callbackURL, _ := f.beginLogin(t, client, "replica-a")
			if test.state {
				callbackURL = changeCallbackState(t, callbackURL, "forged-state")
			}
			response := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil)
			if response.status != http.StatusUnauthorized {
				t.Fatalf("invalid %s callback status = %d, want 401: %s", test.name, response.status, response.body)
			}
			if me := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); me.status != http.StatusUnauthorized {
				t.Fatalf("invalid %s callback created a session: %d %s", test.name, me.status, me.body)
			}
			var loggedIn int
			if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE operator_issuer IS NOT NULL`).Scan(&loggedIn); err != nil || loggedIn != 0 {
				t.Fatalf("logged-in row count after invalid %s = (%d, %v), want 0", test.name, loggedIn, err)
			}
		})
	}
}

func TestSignedOIDCTokenEndpointRefusalDoesNotLeakProviderResponse(t *testing.T) {
	f := newOIDCIntegration(t, 1, oidcSessionPolicy{})
	f.provider.setNext(oidcTestIdentity{subject: "oidc-subject", username: "test-operator"}, "", http.StatusForbidden)
	client := f.browser(t)
	callbackURL, _ := f.beginLogin(t, client, "replica-a")
	response := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil)
	if response.status != http.StatusUnauthorized {
		t.Fatalf("provider refusal status = %d, want 401: %s", response.status, response.body)
	}
	for _, secret := range []string{"test-private-provider-response", "test-access-token", "access_token"} {
		if strings.Contains(response.body, secret) {
			t.Fatalf("provider refusal leaked %q in response: %s", secret, response.body)
		}
	}
	if me := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); me.status != http.StatusUnauthorized {
		t.Fatalf("provider refusal created a session: %d %s", me.status, me.body)
	}
}

func TestOIDCLogoutAndUnsafeRoutesRequireSameOrigin(t *testing.T) {
	f := newOIDCIntegration(t, 1, oidcSessionPolicy{})
	client := f.browser(t)
	callbackURL, _ := f.beginLogin(t, client, "replica-a")
	if response := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil); response.status != http.StatusSeeOther {
		t.Fatalf("login callback status = %d, want 303", response.status)
	}
	assertLoggedIn(t, f, client, "replica-a", "test-operator", "oidc-subject")
	for _, origin := range []string{"", "https://attacker.invalid", f.appURL + "/"} {
		headers := map[string]string{}
		if origin != "" {
			headers["Origin"] = origin
		}
		if response := f.request(t, client, "replica-a", http.MethodPost, f.appURL+"/auth/logout", headers); response.status != http.StatusForbidden {
			t.Errorf("logout Origin %q status = %d, want 403", origin, response.status)
		}
		if response := f.request(t, client, "replica-a", http.MethodPost, f.appURL+"/protected", headers); response.status != http.StatusForbidden {
			t.Errorf("protected mutation Origin %q status = %d, want 403", origin, response.status)
		}
	}
	if response := f.request(t, client, "replica-a", http.MethodPost, f.appURL+"/protected", map[string]string{"Origin": f.appURL}); response.status != http.StatusOK {
		t.Fatalf("same-origin protected mutation status = %d, want 200: %s", response.status, response.body)
	}
	logout := f.request(t, client, "replica-a", http.MethodPost, f.appURL+"/auth/logout", map[string]string{"Origin": f.appURL})
	if logout.status != http.StatusSeeOther {
		t.Fatalf("same-origin logout status = %d, want 303: %s", logout.status, logout.body)
	}
	if response := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusUnauthorized {
		t.Fatalf("session after logout status = %d, want 401", response.status)
	}
}

func TestOIDCSessionIdleAbsoluteExpiryAndActivityStep(t *testing.T) {
	policy := oidcSessionPolicy{maxSeconds: 300, idleSeconds: 120, stepSeconds: 5}
	f := newOIDCIntegration(t, 1, policy)
	client := f.browser(t)
	callbackURL, _ := f.beginLogin(t, client, "replica-a")
	callback := f.request(t, client, "replica-a", http.MethodGet, callbackURL, nil)
	if callback.status != http.StatusSeeOther {
		t.Fatalf("login callback status = %d, want 303", callback.status)
	}
	cookie := responseCookie(t, callback, f.auths[0].cfg.OIDC.CookieName())
	id := cookieRowID(t, f.auths[0], cookie)
	expiresAt, absolute := sessionDeadlines(t, f.dbs[0], id)
	if absolute.Sub(time.Now()) < 299*time.Second || absolute.Sub(time.Now()) > 300*time.Second || !expiresAt.Before(absolute) {
		t.Fatalf("initial session deadlines = (%s, %s), want idle before a 300s absolute deadline", expiresAt, absolute)
	}
	if cookie.MaxAge < 299 || cookie.MaxAge > 300 {
		t.Fatalf("cookie MaxAge = %d, want 299..300 seconds", cookie.MaxAge)
	}

	started := time.Now().UTC()
	idle := started.Add(10 * time.Second)
	setSessionDeadlines(t, f.dbs[0], id, idle, absolute)
	if response := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusOK {
		t.Fatalf("session with active idle deadline status = %d: %s", response.status, response.body)
	}
	extended, _ := sessionDeadlines(t, f.dbs[0], id)
	if extended.Before(started.Add(119*time.Second)) || extended.After(started.Add(122*time.Second)) {
		t.Fatalf("activity extended idle expiry to %s, want about %s", extended, started.Add(120*time.Second))
	}

	belowStep := time.Now().UTC().Add(119 * time.Second)
	setSessionDeadlines(t, f.dbs[0], id, belowStep, absolute)
	if response := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusOK {
		t.Fatalf("session within activity step status = %d", response.status)
	}
	unchanged, _ := sessionDeadlines(t, f.dbs[0], id)
	if delta := unchanged.Sub(belowStep); delta > time.Millisecond || delta < -time.Millisecond {
		t.Fatalf("expiry moved from %s to %s for activity below the 5s write step", belowStep, unchanged)
	}

	clampStarted := time.Now().UTC()
	clampAbsolute := clampStarted.Add(20 * time.Second)
	setSessionDeadlines(t, f.dbs[0], id, clampStarted.Add(5*time.Second), clampAbsolute)
	if response := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusOK {
		t.Fatalf("session before absolute deadline status = %d", response.status)
	}
	clamped, storedAbsolute := sessionDeadlines(t, f.dbs[0], id)
	if !clamped.Equal(storedAbsolute) {
		t.Fatalf("activity deadline = (%s, %s), want idle extension clamped at absolute %s", clamped, storedAbsolute, clampAbsolute)
	}

	idleExpired := time.Now().UTC().Add(-time.Second)
	setSessionDeadlines(t, f.dbs[0], id, idleExpired, time.Now().UTC().Add(time.Hour))
	if response := f.request(t, client, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusUnauthorized {
		t.Fatalf("idle-expired session status = %d, want 401", response.status)
	}
	if count, err := countSessionRows(t, f.dbs[0], id); err != nil || count != 0 {
		t.Fatalf("idle-expired row count = (%d, %v), want 0", count, err)
	}

	absoluteClient := f.browser(t)
	absoluteCallback, _ := f.beginLogin(t, absoluteClient, "replica-a")
	absoluteLogin := f.request(t, absoluteClient, "replica-a", http.MethodGet, absoluteCallback, nil)
	absoluteCookie := responseCookie(t, absoluteLogin, f.auths[0].cfg.OIDC.CookieName())
	absoluteID := cookieRowID(t, f.auths[0], absoluteCookie)
	absoluteExpired := time.Now().UTC().Add(-time.Second)
	setSessionDeadlines(t, f.dbs[0], absoluteID, absoluteExpired, absoluteExpired)
	if response := f.request(t, absoluteClient, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusUnauthorized {
		t.Fatalf("absolute-expired session status = %d, want 401", response.status)
	}
	if count, err := countSessionRows(t, f.dbs[0], absoluteID); err != nil || count != 0 {
		t.Fatalf("absolute-expired row count = (%d, %v), want 0", count, err)
	}
}

func TestOIDCCallbackAndTwoOperatorsWorkAcrossReplicas(t *testing.T) {
	f := newOIDCIntegration(t, 2, oidcSessionPolicy{})
	first := f.browser(t)
	f.provider.setNext(oidcTestIdentity{subject: "subject-a", username: "operator-a"}, "", 0)
	callbackA, _ := f.beginLogin(t, first, "replica-a")
	if response := f.request(t, first, "replica-b", http.MethodGet, callbackA, nil); response.status != http.StatusSeeOther {
		t.Fatalf("replica-b callback for replica-a login = %d: %s", response.status, response.body)
	}
	assertLoggedIn(t, f, first, "replica-a", "operator-a", "subject-a")

	second := f.browser(t)
	f.provider.setNext(oidcTestIdentity{subject: "subject-b", username: "operator-b"}, "", 0)
	callbackB, _ := f.beginLogin(t, second, "replica-b")
	if response := f.request(t, second, "replica-a", http.MethodGet, callbackB, nil); response.status != http.StatusSeeOther {
		t.Fatalf("replica-a callback for replica-b login = %d: %s", response.status, response.body)
	}
	assertLoggedIn(t, f, first, "replica-b", "operator-a", "subject-a")
	assertLoggedIn(t, f, second, "replica-a", "operator-b", "subject-b")
	for _, client := range []*http.Client{first, second} {
		for _, cookie := range client.Jar.Cookies(mustURL(t, f.appURL)) {
			for _, personal := range []string{"operator-a", "operator-b", "subject-a", "subject-b", "test-access-token"} {
				if strings.Contains(cookie.Value, personal) {
					t.Fatalf("browser cookie leaked %q", personal)
				}
			}
			if len(cookie.Value) > 150 {
				t.Fatalf("browser cookie length = %d, want an opaque handle", len(cookie.Value))
			}
		}
	}
	var rows int
	if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE operator_issuer IS NOT NULL`).Scan(&rows); err != nil || rows != 2 {
		t.Fatalf("operator rows = (%d, %v), want two distinct identities", rows, err)
	}
	logout := f.request(t, first, "replica-b", http.MethodPost, f.appURL+"/auth/logout", map[string]string{"Origin": f.appURL})
	if logout.status != http.StatusSeeOther {
		t.Fatalf("cross-replica logout status = %d, want 303", logout.status)
	}
	if response := f.request(t, first, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusUnauthorized {
		t.Fatalf("cross-replica logout left the first session alive: %d", response.status)
	}
	if response := f.request(t, second, "replica-b", http.MethodGet, f.appURL+"/auth/me", nil); response.status != http.StatusOK {
		t.Fatalf("logging out the first operator changed the second session: %d %s", response.status, response.body)
	}
}

func TestConcurrentCrossReplicaCallbacksConsumePendingLoginOnce(t *testing.T) {
	f := newOIDCIntegration(t, 2, oidcSessionPolicy{})
	first, second := f.browser(t), f.browser(t)
	callbackURL, pendingCookie := f.beginLogin(t, first, "replica-a")
	setBrowserCookie(t, second, f.appURL, pendingCookie)
	f.provider.setExchangeBarrier(2)

	type callbackResult struct {
		status int
		body   string
		err    error
	}
	results := make(chan callbackResult, 2)
	for _, item := range []struct {
		client  *http.Client
		replica string
	}{{first, "replica-a"}, {second, "replica-b"}} {
		go func(client *http.Client, replica string) {
			request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, callbackURL, nil)
			if err != nil {
				results <- callbackResult{err: err}
				return
			}
			request.Header.Set("X-Test-Replica", replica)
			response, err := client.Do(request)
			if err != nil {
				results <- callbackResult{err: err}
				return
			}
			defer response.Body.Close()
			body, err := io.ReadAll(response.Body)
			results <- callbackResult{status: response.StatusCode, body: string(body), err: err}
		}(item.client, item.replica)
	}
	statuses := make([]int, 0, 2)
	for range 2 {
		result := <-results
		if result.err != nil {
			t.Fatal(result.err)
		}
		statuses = append(statuses, result.status)
	}
	sort.Ints(statuses)
	if len(statuses) != 2 || statuses[0] != http.StatusSeeOther || statuses[1] != http.StatusUnauthorized {
		t.Fatalf("concurrent callback statuses = %v, want [303 401]", statuses)
	}
	firstStatus := f.request(t, first, "replica-a", http.MethodGet, f.appURL+"/auth/me", nil).status
	secondStatus := f.request(t, second, "replica-b", http.MethodGet, f.appURL+"/auth/me", nil).status
	if (firstStatus == http.StatusOK) == (secondStatus == http.StatusOK) {
		t.Fatalf("concurrent callback sessions have statuses (%d, %d), want exactly one 200", firstStatus, secondStatus)
	}
	var rows int
	if err := f.dbs[0].QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE operator_issuer IS NOT NULL`).Scan(&rows); err != nil || rows != 1 {
		t.Fatalf("operator rows after concurrent callback = (%d, %v), want exactly one", rows, err)
	}
}

func countSessionRows(t *testing.T, db *sql.DB, id string) (int, error) {
	t.Helper()
	var count int
	err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM operator_browser_session WHERE id=$1`, id).Scan(&count)
	return count, err
}

func sessionDeadlines(t *testing.T, db *sql.DB, id string) (time.Time, time.Time) {
	t.Helper()
	var expiresAt, absolute time.Time
	if err := db.QueryRowContext(t.Context(), `SELECT expires_at, absolute_expires_at FROM operator_browser_session WHERE id=$1`, id).Scan(&expiresAt, &absolute); err != nil {
		t.Fatal(err)
	}
	return expiresAt, absolute
}

func setSessionDeadlines(t *testing.T, db *sql.DB, id string, expiresAt, absolute time.Time) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `UPDATE operator_browser_session SET expires_at=$2, absolute_expires_at=$3 WHERE id=$1`, id, expiresAt, absolute); err != nil {
		t.Fatal(err)
	}
}

func mustURL(t *testing.T, value string) *url.URL {
	t.Helper()
	parsed, err := url.Parse(value)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}
