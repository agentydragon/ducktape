package authserver

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	_ "github.com/lib/pq"
	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/oauth2"
	"google.golang.org/protobuf/types/known/emptypb"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	workloadauth "github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth" // its name, auth, is go-sdk's
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}

const (
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline       = 30 * time.Second
	testOperatorIssuer = "https://test-operator-issuer.example"
	testOperator       = "test-operator"
	testOtherOperator  = "test-other-operator"
	testConsentURL     = "https://test-console.example"
	testUpstreamClient = "test-upstream-client"
	testUpstreamSecret = "test-upstream-secret"
	// testUpstreamSubject is the pinned operator's upstream account; testUpstreamStranger is anyone else's.
	testUpstreamSubject  = "test-upstream-operator"
	testUpstreamStranger = "test-upstream-stranger"
	upstreamKeyID        = "test-upstream-key"
	// testRedirectURI is a native client's loopback redirect URI.
	testRedirectURI = "http://127.0.0.1/test-callback"
)

var (
	callerAccount = workloadauth.Account{Namespace: "test-agentplane", Name: "test-caller"}
	otherAccount  = workloadauth.Account{Namespace: "test-agentplane", Name: "test-other-caller"}
)

// testCallers is a current view of the labeled ServiceAccounts that the test sets.
type testCallers struct {
	mu       sync.Mutex
	accounts []workloadauth.Account
}

func (c *testCallers) CallerServiceAccounts() ([]workloadauth.Account, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.accounts), true
}

// label makes the accounts the labeled ones.
func (c *testCallers) label(accounts ...workloadauth.Account) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.accounts = accounts
}

type operatorKey struct{}

// testGate admits every call as the operator of testOperatorIssuer its context names.
type testGate struct{}

func (testGate) Operator(ctx context.Context) (string, string) {
	subject, _ := ctx.Value(operatorKey{}).(string)
	return testOperatorIssuer, subject
}

func (testGate) Recheck(context.Context) error { return nil }

func (testGate) Draining() <-chan struct{} { return nil }

// as is a call context of the operator, bounded by callDeadline.
func as(t *testing.T, subject string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return context.WithValue(ctx, operatorKey{}, subject)
}

// fakeUpstream is the OIDC provider the pinned operator signs in at. As Authentik does, it issues RS256 JWT access
// tokens for its client, and rotates refresh tokens.
type fakeUpstream struct {
	server *httptest.Server
	key    *rsa.PrivateKey
	mu     sync.Mutex
	// subject is the account that signs in, at an authorization and at each refresh.
	subject string
	// lifetime is each access token's.
	lifetime time.Duration
	// failing is the path that answers 503.
	failing string
	// forging signs access tokens with a key its key set lacks.
	forging bool
	// altering, if set, edits the claims of each access token.
	altering func(claims map[string]any)
	// withholding issues no refresh token.
	withholding bool
	// refusing refuses every refresh.
	refusing      bool
	codes         map[string]upstreamCode
	refreshTokens map[string]bool
}

type upstreamCode struct{ challenge, redirectURI, subject string }

func newFakeUpstream(t *testing.T) *fakeUpstream {
	t.Helper()
	u := &fakeUpstream{
		key: newKey(t), subject: testUpstreamSubject, lifetime: 2 * time.Hour, codes: map[string]upstreamCode{},
		refreshTokens: map[string]bool{},
	}
	u.server = httptest.NewServer(http.HandlerFunc(u.serve))
	t.Cleanup(u.server.Close)
	return u
}

func newKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

// change changes how the provider answers from now on.
func (u *fakeUpstream) change(f func(*fakeUpstream)) {
	u.mu.Lock()
	defer u.mu.Unlock()
	f(u)
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (u *fakeUpstream) serve(w http.ResponseWriter, r *http.Request) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if r.URL.Path == u.failing {
		http.Error(w, "test outage", http.StatusServiceUnavailable)
		return
	}
	switch r.URL.Path {
	case "/.well-known/openid-configuration":
		writeJSON(w, http.StatusOK, map[string]any{
			"issuer": u.server.URL, "authorization_endpoint": u.server.URL + "/authorize",
			"token_endpoint": u.server.URL + "/token", "jwks_uri": u.server.URL + "/jwks",
			"response_types_supported": []string{"code"}, "subject_types_supported": []string{"public"},
			"id_token_signing_alg_values_supported": []string{"RS256"},
		})
	case "/jwks":
		writeJSON(w, http.StatusOK, jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
			{Key: &u.key.PublicKey, KeyID: upstreamKeyID, Algorithm: string(jose.RS256), Use: "sig"},
		}})
	case "/authorize":
		u.authorize(w, r)
	case "/token":
		u.token(w, r)
	default:
		http.NotFound(w, r)
	}
}

// authorize signs the browser in as subject at once and sends it back with a code.
func (u *fakeUpstream) authorize(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	target, err := url.Parse(query.Get("redirect_uri"))
	if err != nil || query.Get("client_id") != testUpstreamClient ||
		query.Get("code_challenge_method") != "S256" || query.Get("code_challenge") == "" {
		http.Error(w, "test upstream: malformed authorization request", http.StatusBadRequest)
		return
	}
	code := rand.Text()
	u.codes[code] = upstreamCode{challenge: query.Get("code_challenge"), redirectURI: target.String(), subject: u.subject}
	target.RawQuery = url.Values{"code": {code}, "state": {query.Get("state")}}.Encode()
	http.Redirect(w, r, target.String(), http.StatusFound)
}

func (u *fakeUpstream) token(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_request"})
		return
	}
	id, secret, basic := r.BasicAuth()
	if !basic {
		id, secret = r.PostForm.Get("client_id"), r.PostForm.Get("client_secret")
	}
	if id != testUpstreamClient || secret != testUpstreamSecret {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid_client"})
		return
	}
	var subject string
	switch r.PostForm.Get("grant_type") {
	case "authorization_code":
		code, known := u.codes[r.PostForm.Get("code")]
		delete(u.codes, r.PostForm.Get("code"))
		if !known || oauth2.S256ChallengeFromVerifier(r.PostForm.Get("code_verifier")) != code.challenge ||
			r.PostForm.Get("redirect_uri") != code.redirectURI {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
			return
		}
		subject = code.subject
	case "refresh_token":
		token := r.PostForm.Get("refresh_token")
		if u.refusing || !u.refreshTokens[token] {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
			return
		}
		delete(u.refreshTokens, token)
		subject = u.subject
	default:
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unsupported_grant_type"})
		return
	}
	access, err := u.accessToken(subject)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	answer := map[string]any{"access_token": access, "token_type": "Bearer", "expires_in": int(u.lifetime / time.Second)}
	if !u.withholding {
		refresh := rand.Text()
		u.refreshTokens[refresh] = true
		answer["refresh_token"] = refresh
	}
	writeJSON(w, http.StatusOK, answer)
}

func (u *fakeUpstream) accessToken(subject string) (string, error) {
	key := u.key
	if u.forging {
		forged, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			return "", err
		}
		key = forged
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", upstreamKeyID))
	if err != nil {
		return "", err
	}
	now := time.Now()
	claims := map[string]any{
		"iss": u.server.URL, "sub": subject, "aud": testUpstreamClient, "iat": now.Unix(), "exp": now.Add(u.lifetime).Unix(),
	}
	if u.altering != nil {
		u.altering(claims)
	}
	return jwt.Signed(signer).Claims(claims).Serialize()
}

// documentServer serves client metadata documents over https.
type documentServer struct {
	server   *httptest.Server
	mu       sync.Mutex
	handlers map[string]http.Handler
}

func newDocumentServer(t *testing.T) *documentServer {
	t.Helper()
	d := &documentServer{handlers: map[string]http.Handler{}}
	d.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		handler, found := d.handlers[r.URL.Path]
		d.mu.Unlock()
		if !found {
			http.NotFound(w, r)
			return
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(d.server.Close)
	return d
}

// handle serves the path with the handler, and returns its URL.
func (d *documentServer) handle(path string, handler http.Handler) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.handlers[path] = handler
	return d.server.URL + path
}

func (d *documentServer) publish(path string, body []byte) string {
	return d.handle(path, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
}

// harness is the authorization server on a database of its own, with /mcp beside it, the operator's consent through
// the ConnectionService, the upstream provider and a server of client metadata documents.
type harness struct {
	db          *sql.DB
	callers     *testCallers
	connections *connections.Service
	upstream    *fakeUpstream
	documents   *documentServer
	server      *Server
	// url is the issuer, where the endpoints and /mcp are served.
	url string
	// browser follows no redirect, so each step of a flow is seen.
	browser *http.Client
}

func start(t *testing.T, configure ...func(*Config)) *harness {
	t.Helper()
	return startWithResource(t, func(server *Server) http.Handler {
		return auth.RequireBearerToken(server.VerifyBearer, &auth.RequireBearerTokenOptions{
			ResourceMetadataURL: server.ResourceMetadataURL(),
		})(whoami())
	}, configure...)
}

func startWithResource(t *testing.T, resource func(*Server) http.Handler, configure ...func(*Config)) *harness {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	h := &harness{
		db: db, callers: &testCallers{accounts: []workloadauth.Account{callerAccount, otherAccount}},
		upstream: newFakeUpstream(t), documents: newDocumentServer(t),
		browser: &http.Client{
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
			Timeout:       callDeadline,
		},
	}
	store := connections.NewStore(db, h.callers)
	h.connections = connections.NewService(store, testGate{}, nil)
	listener := httptest.NewUnstartedServer(nil)
	h.url = "http://" + listener.Listener.Addr().String()
	config := Config{
		Issuer: h.url, ConsentURL: testConsentURL, UpstreamIssuer: h.upstream.server.URL,
		UpstreamClientID: testUpstreamClient, UpstreamClientSecret: testUpstreamSecret, UpstreamSubject: testUpstreamSubject,
		Approver:       connections.Operator{Issuer: testOperatorIssuer, Subject: testOperator},
		DocumentClient: h.documents.server.Client(),
	}
	for _, c := range configure {
		c(&config)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	if h.server, err = New(ctx, config, db, store); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.Handle(resourcePath, resource(h.server))
	mux.Handle("/", h.server.Handler())
	listener.Config.Handler = mux
	listener.Start()
	t.Cleanup(listener.Close)
	return h
}

// whoami is an MCP server whose one tool, whoami, answers the account the call was admitted as.
func whoami() http.Handler {
	server := mcp.NewServer(&mcp.Implementation{Name: "test-resource"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "whoami"},
		func(_ context.Context, request *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, any, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: request.Extra.TokenInfo.UserID}}}, nil, nil
		})
	return mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{Stateless: true})
}

func userID(account workloadauth.Account) string { return account.Namespace + "/" + account.Name }

// must fails the test on err. It takes t last, so a call's results can be its arguments: must(f())(t).
func must[T any](value T, err error) func(*testing.T) T {
	return func(t *testing.T) T {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
}

// must2 is must of a call that returns two values.
func must2[T, U any](first T, second U, err error) func(*testing.T) (T, U) {
	return func(t *testing.T) (T, U) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
		return first, second
	}
}

func (h *harness) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := h.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

// request is an authorization request a client makes.
type request struct {
	clientID, redirectURI, verifier, state string
}

func newRequest(clientID string) request {
	return request{clientID: clientID, redirectURI: testRedirectURI, verifier: oauth2.GenerateVerifier(), state: rand.Text()}
}

// authorizeURL is where the client sends the browser for the request; change edits its parameters.
func (h *harness) authorizeURL(r request, change func(url.Values)) string {
	query := url.Values{
		"response_type": {"code"}, "client_id": {r.clientID}, "redirect_uri": {r.redirectURI}, "state": {r.state},
		"code_challenge": {oauth2.S256ChallengeFromVerifier(r.verifier)}, "code_challenge_method": {"S256"},
		"resource": {h.url + resourcePath},
	}
	if change != nil {
		change(query)
	}
	return h.url + authorizePath + "?" + query.Encode()
}

// load is the browser loading the URL.
func (h *harness) load(rawURL string) (*http.Response, string, error) {
	response, err := h.browser.Get(rawURL)
	if err != nil {
		return nil, "", err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	return response, string(body), err
}

// follow loads the URL, which must answer with a redirect, and returns where to.
func (h *harness) follow(rawURL string) (string, error) {
	response, body, err := h.load(rawURL)
	if err != nil {
		return "", err
	}
	if response.StatusCode != http.StatusFound {
		return "", fmt.Errorf("GET %s: %s %s, want a redirect", rawURL, response.Status, body)
	}
	return response.Header.Get("Location"), nil
}

const consentPrefix = testConsentURL + "/#/connection-enrollments/"

// newConnection allows a new Connection bound to the account.
func newConnection(account workloadauth.Account) *pb.EnrollmentAllow {
	return &pb.EnrollmentAllow{
		ServiceAccount: &sandboxes.ServiceAccount{Namespace: account.Namespace, Name: account.Name},
		Connection: &pb.EnrollmentAllow_NewConnection{
			NewConnection: &pb.NewConnection{DisplayName: "test-connection"},
		},
	}
}

// reconnection allows the Connection, as it is now, to be bound to the account.
func reconnection(account workloadauth.Account, c *pb.Connection) *pb.EnrollmentAllow {
	allow := newConnection(account)
	allow.Connection = &pb.EnrollmentAllow_Reconnect{Reconnect: &pb.ReconnectConnection{
		ConnectionId: c.GetId(), ExpectedVersion: c.GetVersion(), AuthorityChangeConfirmed: true,
	}}
	return allow
}

// consent follows the authorization request to its consent page, where the operator decides as allow says, or denies
// when it is nil.
func (h *harness) consent(ctx context.Context, authorizeURL, operator string, allow *pb.EnrollmentAllow) (*pb.DecideConnectionEnrollmentResponse, error) {
	location, err := h.follow(authorizeURL)
	if err != nil {
		return nil, err
	}
	handle, found := strings.CutPrefix(location, consentPrefix)
	if !found {
		return nil, fmt.Errorf("the authorization request redirected to %s, want consent", location)
	}
	ctx = context.WithValue(ctx, operatorKey{}, operator)
	preview, err := h.connections.PreviewConnectionEnrollment(ctx, &pb.PreviewConnectionEnrollmentRequest{Handle: handle})
	if err != nil {
		return nil, err
	}
	decision := &pb.DecideConnectionEnrollmentRequest{
		Handle: handle, ExpectedVersion: preview.GetVersion(),
		Decision: &pb.DecideConnectionEnrollmentRequest_Deny{Deny: &emptypb.Empty{}},
	}
	if allow != nil {
		decision.Decision = &pb.DecideConnectionEnrollmentRequest_Allow{Allow: allow}
	}
	return h.connections.DecideConnectionEnrollment(ctx, decision)
}

// signIn is the browser signing in upstream from where consent sent it, and arriving back at the callback: the
// redirect to the client the callback answers.
func (h *harness) signIn(upstreamURL string) (*url.URL, error) {
	callback, err := h.follow(upstreamURL)
	if err != nil {
		return nil, err
	}
	location, err := h.follow(callback)
	if err != nil {
		return nil, err
	}
	return url.Parse(location)
}

// authorize runs the browser through testOperator's consent, as allow says, and the upstream sign-in.
func (h *harness) authorize(ctx context.Context, authorizeURL string, allow *pb.EnrollmentAllow) (*url.URL, error) {
	decided, err := h.consent(ctx, authorizeURL, testOperator, allow)
	if err != nil {
		return nil, err
	}
	return h.signIn(decided.GetRedirectUrl())
}

// fetcher is an MCP client's way through the browser: testOperator allows a new Connection bound to callerAccount.
func (h *harness) fetcher() auth.AuthorizationCodeFetcher {
	return func(ctx context.Context, args *auth.AuthorizationArgs) (*auth.AuthorizationResult, error) {
		location, err := h.authorize(ctx, args.URL, newConnection(callerAccount))
		if err != nil {
			return nil, err
		}
		answer := location.Query()
		return &auth.AuthorizationResult{Code: answer.Get("code"), State: answer.Get("state"), Iss: answer.Get("iss")}, nil
	}
}

// answer is the authorization response the redirect delivers: it must reach the request's redirect URI with its state
// and the issuer, in the query or, if the client asked for it, the fragment. A redirect elsewhere delivers none.
func (h *harness) answer(t *testing.T, r request, location *url.URL) url.Values {
	t.Helper()
	params := location.Query()
	if location.Fragment != "" {
		params = must(url.ParseQuery(location.Fragment))(t)
	}
	target := *location
	target.RawQuery, target.Fragment = "", ""
	if target.String() != r.redirectURI || params.Get("state") != r.state || params.Get("iss") != h.url {
		t.Errorf("redirected to %s, want %s with the state and issuer", location, r.redirectURI)
		return nil
	}
	return params
}

// answerAt loads the URL, which must redirect the browser to the request's client, and returns the authorization
// response it delivers.
func (h *harness) answerAt(t *testing.T, r request, rawURL string) url.Values {
	t.Helper()
	return h.answer(t, r, must(url.Parse(must(h.follow(rawURL))(t)))(t))
}

// code is the code the client receives once testOperator allows a new Connection bound to callerAccount.
func (h *harness) code(t *testing.T, r request) string {
	t.Helper()
	answer := h.answer(t, r, must(h.authorize(t.Context(), h.authorizeURL(r, nil), newConnection(callerAccount)))(t))
	if answer.Get("code") == "" {
		t.Fatalf("answer %v, want a code", answer)
	}
	return answer.Get("code")
}

func wantError(t *testing.T, what string, answer url.Values, code, description string) {
	t.Helper()
	if answer.Get("error") != code || description != "" && answer.Get("error_description") != description ||
		answer.Has("code") {
		t.Errorf("%s: answered %v, want %s %q", what, answer, code, description)
	}
}

// tokenAnswer is an answer of the token or revocation endpoint.
type tokenAnswer struct {
	status       int
	header       http.Header
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int64  `json:"expires_in"`
	Scope        string `json:"scope"`
	Error        string `json:"error"`
	Description  string `json:"error_description"`
}

func (h *harness) post(path string, form url.Values) (tokenAnswer, error) {
	response, err := h.browser.PostForm(h.url+path, form)
	if err != nil {
		return tokenAnswer{}, err
	}
	defer response.Body.Close()
	answer := tokenAnswer{status: response.StatusCode, header: response.Header}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return tokenAnswer{}, err
	}
	if len(body) > 0 {
		if err := json.Unmarshal(body, &answer); err != nil {
			return tokenAnswer{}, fmt.Errorf("POST %s: %s %s", path, response.Status, body)
		}
	}
	return answer, nil
}

// exchangeForm redeems the code as the request's client.
func (h *harness) exchangeForm(r request, code string) url.Values {
	return url.Values{
		"grant_type": {"authorization_code"}, "code": {code}, "redirect_uri": {r.redirectURI}, "client_id": {r.clientID},
		"code_verifier": {r.verifier}, "resource": {h.url + resourcePath},
	}
}

// exchange redeems the code as the request's client; change edits the form.
func (h *harness) exchange(t *testing.T, r request, code string, change func(url.Values)) tokenAnswer {
	t.Helper()
	form := h.exchangeForm(r, code)
	if change != nil {
		change(form)
	}
	return must(h.post(tokenPath, form))(t)
}

func (h *harness) refresh(t *testing.T, clientID, token string) tokenAnswer {
	t.Helper()
	return must(h.post(tokenPath, url.Values{
		"grant_type": {"refresh_token"}, "client_id": {clientID}, "refresh_token": {token},
	}))(t)
}

func (h *harness) revoke(t *testing.T, clientID, token string) tokenAnswer {
	t.Helper()
	return must(h.post(revocationPath, url.Values{"client_id": {clientID}, "token": {token}}))(t)
}

// issued is a successful exchange or refresh: tokens of the actions scope, which no cache may keep.
func issued(t *testing.T, what string, answer tokenAnswer) tokenAnswer {
	t.Helper()
	if answer.status != http.StatusOK || answer.AccessToken == "" || answer.TokenType != "Bearer" ||
		answer.Scope != scopeActions || answer.header.Get("Cache-Control") != "no-store" {
		t.Fatalf("%s: %+v, want tokens", what, answer)
	}
	return answer
}

func wantRefused(t *testing.T, what string, answer tokenAnswer, status int, code, description string) {
	t.Helper()
	if answer.status != status || answer.Error != code || description != "" && answer.Description != description ||
		answer.AccessToken != "" {
		t.Errorf("%s: %+v, want %d %s %q", what, answer, status, code, description)
	}
}

// tokens are the tokens a fresh authorization of the client earns.
func (h *harness) tokens(t *testing.T, clientID string) tokenAnswer {
	t.Helper()
	r := newRequest(clientID)
	return issued(t, "the exchange", h.exchange(t, r, h.code(t, r), nil))
}

func (h *harness) wantAdmitted(t *testing.T, what, token string, account workloadauth.Account) {
	t.Helper()
	info, err := h.server.VerifyBearer(t.Context(), token, nil)
	if err != nil || info.UserID != userID(account) || !slices.Equal(info.Scopes, []string{scopeActions}) {
		t.Errorf("%s: admitted %+v, %v; want %s", what, info, err, userID(account))
	}
}

func (h *harness) wantInvalid(t *testing.T, what, token string) {
	t.Helper()
	if info, err := h.server.VerifyBearer(t.Context(), token, nil); !errors.Is(err, auth.ErrInvalidToken) {
		t.Errorf("%s: admitted %+v, %v; want an invalid token", what, info, err)
	}
}

// register is a Dynamic Client Registration of the metadata.
func (h *harness) register(t *testing.T, metadata any) (int, map[string]any) {
	t.Helper()
	response, err := h.browser.Post(h.url+registrationPath, "application/json",
		strings.NewReader(string(must(json.Marshal(metadata))(t))))
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

// registered is the client_id of a native client registered with the grant types.
func (h *harness) registered(t *testing.T, grantTypes ...string) string {
	t.Helper()
	status, answer := h.register(t, map[string]any{
		"redirect_uris": []string{testRedirectURI}, "client_name": "Test DCR client", "grant_types": grantTypes,
	})
	clientID, _ := answer["client_id"].(string)
	if status != http.StatusCreated || clientID == "" {
		t.Fatalf("registration: %d %v", status, answer)
	}
	return clientID
}

// client is a registered native client that may refresh.
func (h *harness) client(t *testing.T) string {
	t.Helper()
	return h.registered(t, "authorization_code", "refresh_token")
}

// document is the metadata document of a native client that may refresh, naming the client_id, edited by change.
func document(t *testing.T, clientID string, change func(map[string]any)) []byte {
	t.Helper()
	document := map[string]any{
		"client_id": clientID, "client_name": "Test CIMD client", "redirect_uris": []string{testRedirectURI},
		"grant_types": []string{"authorization_code", "refresh_token"}, "response_types": []string{"code"},
		"token_endpoint_auth_method": "none",
	}
	if change != nil {
		change(document)
	}
	return must(json.Marshal(document))(t)
}

// documentClient publishes the client's document at the path, and returns its client_id.
func (h *harness) documentClient(t *testing.T, path string, change func(map[string]any)) string {
	t.Helper()
	return h.documents.publish(path, document(t, h.documents.server.URL+path, change))
}

// connections are the Connections, oldest first.
func (h *harness) listConnections(t *testing.T) []*pb.Connection {
	t.Helper()
	return must(h.connections.ListConnections(as(t, testOperator), &pb.ListConnectionsRequest{}))(t).GetConnections()
}

// only is the one Connection.
func (h *harness) only(t *testing.T) *pb.Connection {
	t.Helper()
	listed := h.listConnections(t)
	if len(listed) != 1 {
		t.Fatalf("%d Connections, want one", len(listed))
	}
	return listed[0]
}
