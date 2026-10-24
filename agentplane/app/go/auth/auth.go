package auth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	workloadauth "github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type Auth struct {
	cfg      Config
	oidc     *openID
	oidcMu   sync.Mutex
	store    *sessionStore
	signer   *cookieSigner
	changes  ChangeSource
	subjects map[string]struct{}
}

// New constructs the auth boundary. OIDC discovery is deferred until a browser uses the login route.
func New(cfg Config) (*Auth, error) {
	return NewWithContext(context.Background(), cfg)
}

func NewWithContext(_ context.Context, cfg Config) (*Auth, error) {
	a := &Auth{cfg: cfg, changes: cfg.SessionChanges, subjects: make(map[string]struct{}, len(cfg.TokenSubjects))}
	for _, subject := range cfg.TokenSubjects {
		if subject == "" {
			return nil, errors.New("token subject allowlist entries must not be empty")
		}
		a.subjects[subject] = struct{}{}
	}
	if cfg.TokenAudience != "" && cfg.TokenReviews == nil {
		return nil, errors.New("TokenReview client is required when token audience is set")
	}
	if cfg.OIDC == nil {
		return a, nil
	}
	settings := cfg.OIDC.withDefaults()
	if err := settings.validate(); err != nil {
		return nil, err
	}
	if cfg.Database == nil {
		return nil, errors.New("PostgreSQL database is required when OIDC is enabled")
	}
	a.cfg.OIDC = &settings
	a.store = &sessionStore{
		db: cfg.Database, idle: time.Duration(settings.SessionIdleSeconds) * time.Second,
		step: time.Duration(settings.SessionActivityStepSeconds) * time.Second, now: time.Now,
	}
	a.signer = newCookieSigner(settings.SessionSecret, settings.SessionMaxSeconds)
	return a, nil
}

func (a *Auth) openIDClient(ctx context.Context) (*openID, error) {
	a.oidcMu.Lock()
	defer a.oidcMu.Unlock()
	if a.oidc != nil {
		return a.oidc, nil
	}
	if a.cfg.OIDC == nil {
		return nil, errors.New("OIDC is not configured")
	}
	providerCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if a.cfg.HTTPClient != nil {
		providerCtx = oidc.ClientContext(providerCtx, a.cfg.HTTPClient)
	}
	settings := *a.cfg.OIDC
	provider, err := oidc.NewProvider(providerCtx, strings.TrimRight(settings.Issuer, "/"))
	if err != nil {
		return nil, fmt.Errorf("discover OIDC provider: %w", err)
	}
	a.oidc = &openID{
		provider: provider,
		verifier: provider.Verifier(&oidc.Config{ClientID: settings.ClientID}),
		oauth: oauth2.Config{
			ClientID: settings.ClientID, ClientSecret: settings.ClientSecret,
			Endpoint:    provider.Endpoint(),
			RedirectURL: settings.RedirectURI(),
			Scopes:      []string{oidc.ScopeOpenID, "profile", "offline_access"},
		},
	}
	return a.oidc, nil
}

// RegisterRoutes mounts the public OIDC endpoints. It is a no-op when OIDC is not configured.
func (a *Auth) RegisterRoutes(mux *http.ServeMux) {
	if a.cfg.OIDC == nil {
		return
	}
	mux.HandleFunc("GET /auth/login", a.login)
	mux.HandleFunc("GET /auth/callback", a.callback)
	mux.HandleFunc("POST /auth/logout", a.logout)
	mux.HandleFunc("GET /auth/me", a.me)
}

// Middleware loads the operator session and commits its key-level changes before response headers
// are written. The response writer retains Flush for streaming handlers.
func (a *Auth) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.store == nil {
			next.ServeHTTP(w, r)
			return
		}
		requestSession, cookie, row, initial, err := a.loadSession(r)
		if err != nil {
			http.Error(w, "authentication session unavailable", http.StatusInternalServerError)
			return
		}
		r = r.WithContext(context.WithValue(r.Context(), sessionContextKey{}, requestSession))
		wrapped := &sessionResponseWriter{ResponseWriter: w, commit: func() error {
			return a.commitSession(r, w, requestSession, cookie, row, initial)
		}}
		next.ServeHTTP(wrapped, r)
		if !wrapped.committed {
			if err := wrapped.commitSession(); err != nil {
				// With no body written, this is still a replaceable response.
				for key := range w.Header() {
					w.Header().Del(key)
				}
				writeCommitError(w, err)
			}
		}
	})
}

// RequireCaller protects a route with either an operator cookie or a reviewed Kubernetes token.
func (a *Auth) RequireCaller(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		caller, status, message := a.caller(r)
		if caller == nil {
			writeError(w, status, message)
			return
		}
		if caller.Kind == CallerOperator && !safeMethod(r.Method) {
			expected := strings.TrimRight(a.cfg.OIDC.PublicBaseURL, "/")
			if r.Header.Get("Origin") != expected {
				writeError(w, http.StatusForbidden, "cross-origin request")
				return
			}
		}
		r = r.WithContext(context.WithValue(r.Context(), callerContextKey{}, *caller))
		next.ServeHTTP(w, r)
	})
}

// CallerFromContext returns the authenticated identity set by RequireCaller.
func CallerFromContext(ctx context.Context) (Caller, bool) {
	caller, ok := ctx.Value(callerContextKey{}).(Caller)
	return caller, ok
}

// SessionFromContext returns the mutable browser-session snapshot set by Middleware.
func SessionFromContext(ctx context.Context) (*RequestSession, bool) {
	session, ok := ctx.Value(sessionContextKey{}).(*RequestSession)
	return session, ok
}

// UntilEnded returns when the named browser row is deleted or expires. rowID is the opaque
// session handle's SHA-256 hex key, as passed by request session accessors.
func (a *Auth) UntilEnded(ctx context.Context, rowID string) error {
	if a.store == nil {
		return errors.New("OIDC sessions are not configured")
	}
	return a.store.untilEnded(ctx, rowID, a.changes)
}

type CallerKind string

const (
	CallerOperator CallerKind = "operator"
	CallerToken    CallerKind = "token"
)

type Caller struct {
	Kind      CallerKind
	Principal string
}

type (
	callerContextKey  struct{}
	sessionContextKey struct{}
)

type RequestSession struct {
	Payload map[string]any
	Login   *OperatorSession
	Row     *SessionRow
	replace *OperatorSession
	logout  bool
}

// LogIn replaces this pending login's session with a fresh, logged-in row.
func (s *RequestSession) LogIn(login OperatorSession) {
	s.Payload = map[string]any{}
	s.replace = &login
	s.logout = false
}

// End clears the browser session at response commit.
func (s *RequestSession) End() {
	s.Payload = map[string]any{}
	s.replace = nil
	s.logout = true
}

type sessionResponseWriter struct {
	http.ResponseWriter
	commit      func() error
	committed   bool
	commitError error
	status      int
	override    bool
}

func (w *sessionResponseWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

func (w *sessionResponseWriter) commitSession() error {
	if !w.committed {
		w.committed = true
		w.commitError = w.commit()
	}
	return w.commitError
}

func (w *sessionResponseWriter) WriteHeader(status int) {
	if w.status != 0 {
		return
	}
	w.status = status
	if err := w.commitSession(); err != nil {
		w.override = true
		writeCommitError(w.ResponseWriter, err)
		return
	}
	w.ResponseWriter.WriteHeader(status)
}

func (w *sessionResponseWriter) Write(data []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if w.override {
		return len(data), nil
	}
	if w.commitError != nil {
		return 0, w.commitError
	}
	return w.ResponseWriter.Write(data)
}

func (w *sessionResponseWriter) Flush() {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if w.commitError == nil && !w.override {
		if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
			flusher.Flush()
		}
	}
}

func (a *Auth) loadSession(r *http.Request) (*RequestSession, *http.Cookie, *browserRow, map[string]any, error) {
	initial := map[string]any{}
	var cookie *http.Cookie
	var row *browserRow
	var rowKey string
	if parsed, err := r.Cookie(a.cfg.OIDC.CookieName()); err == nil {
		cookie = parsed
		if handle, err := a.signer.unsign(parsed.Value); err == nil {
			rowKey = rowID(handle)
			row, err = a.store.read(r.Context(), rowKey)
			if err != nil {
				return nil, cookie, nil, nil, err
			}
		}
	}
	if row != nil {
		initial = clonePayload(row.payload)
	}
	session := &RequestSession{Payload: clonePayload(initial)}
	if row != nil {
		session.Login = row.login
		session.Row = &SessionRow{store: a.store, id: rowKey}
	}
	return session, cookie, row, initial, nil
}

func (a *Auth) commitSession(r *http.Request, w http.ResponseWriter, session *RequestSession, cookie *http.Cookie, row *browserRow, initial map[string]any) error {
	if session.logout {
		if row != nil {
			if err := a.store.end(r.Context(), row.id); err != nil {
				return err
			}
		}
		a.forgetCookie(w)
	} else if session.replace != nil {
		absolute := time.Now().UTC().Add(time.Duration(a.cfg.OIDC.SessionMaxSeconds) * time.Second)
		pending := ""
		if row != nil {
			pending = row.id
		}
		handle, inserted, err := a.store.logIn(r.Context(), pending, session.Payload, *session.replace, absolute)
		if err != nil {
			return err
		}
		if !inserted {
			a.forgetCookie(w)
			return &responseError{status: http.StatusUnauthorized, message: "this login was completed or ended; log in again"}
		}
		if err := a.rememberCookie(w, handle, absolute); err != nil {
			return err
		}
	} else if !payloadEqual(initial, session.Payload) {
		if row == nil {
			pendingTTL := pendingLoginTTL
			maxAge := time.Duration(a.cfg.OIDC.SessionMaxSeconds) * time.Second
			if pendingTTL > maxAge {
				pendingTTL = maxAge
			}
			absolute := time.Now().UTC().Add(pendingTTL)
			handle, err := a.store.create(r.Context(), session.Payload, absolute)
			if err != nil {
				return err
			}
			if err := a.rememberCookie(w, handle, absolute); err != nil {
				return err
			}
		} else {
			saved, err := a.store.save(r.Context(), row.id, initial, session.Payload)
			if err != nil {
				return err
			}
			if !saved {
				a.forgetCookie(w)
			}
		}
	} else if cookie != nil && row == nil {
		a.forgetCookie(w)
	}
	if w.Header().Get("Cache-Control") == "" {
		w.Header().Set("Cache-Control", "no-store")
	}
	return nil
}

func (a *Auth) rememberCookie(w http.ResponseWriter, handle string, absolute time.Time) error {
	maxAge := int(time.Until(absolute).Seconds())
	if maxAge < 0 {
		maxAge = 0
	}
	value, err := a.signer.sign(handle)
	if err != nil {
		return fmt.Errorf("sign session cookie: %w", err)
	}
	http.SetCookie(w, &http.Cookie{Name: a.cfg.OIDC.CookieName(), Value: value, Path: "/", MaxAge: maxAge, HttpOnly: true, Secure: a.cfg.OIDC.Secure(), SameSite: http.SameSiteLaxMode})
	return nil
}

func (a *Auth) forgetCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{Name: a.cfg.OIDC.CookieName(), Value: "", Path: "/", MaxAge: -1, Expires: time.Unix(1, 0), HttpOnly: true, Secure: a.cfg.OIDC.Secure(), SameSite: http.SameSiteLaxMode})
}

func (a *Auth) login(w http.ResponseWriter, r *http.Request) {
	session, ok := SessionFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusInternalServerError, "session middleware is required")
		return
	}
	provider, err := a.openIDClient(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "identity provider unavailable")
		return
	}
	state, err := randomURLToken(32)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not start login")
		return
	}
	nonce, err := randomURLToken(32)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not start login")
		return
	}
	verifier := oauth2.GenerateVerifier()
	challenge := oauth2.S256ChallengeFromVerifier(verifier)
	url := provider.oauth.AuthCodeURL(state, oauth2.AccessTypeOffline, oauth2.SetAuthURLParam("code_challenge", challenge), oauth2.SetAuthURLParam("code_challenge_method", "S256"), oauth2.SetAuthURLParam("nonce", nonce))
	stateData := map[string]any{
		"data": map[string]any{"redirect_uri": a.cfg.OIDC.RedirectURI(), "url": url, "nonce": nonce, "code_verifier": verifier},
		"exp":  float64(time.Now().Add(pendingLoginTTL).Unix()),
	}
	session.Payload[loginStatePrefix+state] = stateData
	http.Redirect(w, r, url, http.StatusFound)
}

func (a *Auth) callback(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("error") != "" {
		writeError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	state := r.URL.Query().Get("state")
	session, ok := SessionFromContext(r.Context())
	if !ok || state == "" {
		writeError(w, http.StatusUnauthorized, "OIDC state is missing")
		return
	}
	key := loginStatePrefix + state
	stateValue, exists := session.Payload[key]
	if !exists {
		writeError(w, http.StatusUnauthorized, "OIDC state is invalid or expired")
		return
	}
	data, expiry, err := parseState(stateValue)
	if err != nil || !expiry.After(time.Now()) {
		writeError(w, http.StatusUnauthorized, "OIDC state is invalid or expired")
		return
	}
	code := r.URL.Query().Get("code")
	if code == "" {
		writeError(w, http.StatusUnauthorized, "OIDC authorization code is missing")
		return
	}
	provider, err := a.openIDClient(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "identity provider unavailable")
		return
	}
	delete(session.Payload, key)
	ctx := r.Context()
	if a.cfg.HTTPClient != nil {
		ctx = oidc.ClientContext(ctx, a.cfg.HTTPClient)
	}
	token, err := provider.oauth.Exchange(ctx, code, oauth2.SetAuthURLParam("code_verifier", data.CodeVerifier))
	if err != nil {
		writeError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	if token.AccessToken == "" {
		writeError(w, http.StatusBadGateway, "identity provider returned an invalid response")
		return
	}
	rawIDToken, ok := token.Extra("id_token").(string)
	if !ok || rawIDToken == "" {
		writeError(w, http.StatusBadGateway, "identity provider returned an invalid response")
		return
	}
	idToken, err := provider.verifier.Verify(ctx, rawIDToken)
	if err != nil || idToken.Nonce != data.Nonce {
		writeError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	claims := struct {
		Issuer          string          `json:"iss"`
		Subject         string          `json:"sub"`
		Username        string          `json:"preferred_username"`
		AuthorizedParty string          `json:"azp"`
		Audience        json.RawMessage `json:"aud"`
	}{}
	if err := idToken.Claims(&claims); err != nil {
		writeError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	if claims.Issuer != a.cfg.OIDC.Issuer || claims.Subject == "" || claims.Username == "" || (claims.AuthorizedParty != "" && claims.AuthorizedParty != a.cfg.OIDC.ClientID) || !audienceIsExact(claims.Audience, a.cfg.OIDC.ClientID) || !idToken.Expiry.After(time.Now()) {
		writeError(w, http.StatusUnauthorized, "OIDC identity is not accepted")
		return
	}
	var loginTokens *LoginTokens
	if a.cfg.StoreFederationTokens && token.AccessToken != "" && !token.Expiry.IsZero() && token.Expiry.After(time.Now()) {
		loginTokens = &LoginTokens{AccessToken: token.AccessToken, ExpiresAt: token.Expiry}
		if refresh, ok := token.Extra("refresh_token").(string); ok {
			loginTokens.RefreshToken = refresh
		}
	}
	session.LogIn(OperatorSession{Issuer: claims.Issuer, Subject: claims.Subject, Username: claims.Username, Tokens: loginTokens})
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func (a *Auth) logout(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != strings.TrimRight(a.cfg.OIDC.PublicBaseURL, "/") {
		writeError(w, http.StatusForbidden, "logout requires exact same-origin Origin")
		return
	}
	if session, ok := SessionFromContext(r.Context()); ok {
		session.End()
	}
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func (a *Auth) me(w http.ResponseWriter, r *http.Request) {
	session, ok := SessionFromContext(r.Context())
	if !ok || session.Login == nil {
		writeError(w, http.StatusUnauthorized, "not logged in")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"username": session.Login.Username})
}

func (a *Auth) caller(r *http.Request) (*Caller, int, string) {
	if session, ok := SessionFromContext(r.Context()); ok && session.Login != nil {
		return &Caller{Kind: CallerOperator, Principal: session.Login.Username}, 0, ""
	}
	if a.cfg.TokenReviews == nil || a.cfg.TokenAudience == "" {
		return nil, http.StatusUnauthorized, "no session and no accepted token"
	}
	fields := strings.Fields(r.Header.Get("Authorization"))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "Bearer") {
		return nil, http.StatusUnauthorized, "no session and no accepted token"
	}
	return a.tokenCaller(r, fields[1])
}

func (a *Auth) tokenCaller(r *http.Request, token string) (*Caller, int, string) {
	return reviewToken(r.Context(), a.cfg.TokenReviews, a.cfg.TokenAudience, a.subjects, token)
}

func reviewToken(ctx context.Context, reviews workloadauth.TokenReviews, audience string, subjects map[string]struct{}, bearer string) (*Caller, int, string) {
	review, err := reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: bearer, Audiences: []string{audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, http.StatusServiceUnavailable, "token review unavailable"
	}
	if review == nil || !review.Status.Authenticated || review.Status.User.Username == "" || !contains(review.Status.Audiences, audience) {
		return nil, http.StatusUnauthorized, "no session and no accepted token"
	}
	username := review.Status.User.Username
	if _, ok := subjects[username]; !ok {
		return nil, http.StatusForbidden, "token subject is not accepted here"
	}
	return &Caller{Kind: CallerToken, Principal: username}, 0, ""
}

type parsedState struct{ Nonce, CodeVerifier string }

func parseUnixSeconds(value any) (time.Time, bool) {
	switch typed := value.(type) {
	case float64:
		return time.Unix(int64(typed), int64((typed-float64(int64(typed)))*1e9)), true
	case json.Number:
		seconds, err := strconv.ParseFloat(typed.String(), 64)
		if err == nil {
			return time.Unix(int64(seconds), int64((seconds-float64(int64(seconds)))*1e9)), true
		}
	case int64:
		return time.Unix(typed, 0), true
	case int:
		return time.Unix(int64(typed), 0), true
	}
	return time.Time{}, false
}

func parseState(value any) (parsedState, time.Time, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return parsedState{}, time.Time{}, errors.New("state is not an object")
	}
	data, ok := object["data"].(map[string]any)
	if !ok {
		return parsedState{}, time.Time{}, errors.New("state data is missing")
	}
	nonce, _ := data["nonce"].(string)
	verifier, _ := data["code_verifier"].(string)
	expiry, ok := parseUnixSeconds(object["exp"])
	if !ok || nonce == "" || verifier == "" {
		return parsedState{}, time.Time{}, errors.New("state data is incomplete")
	}
	return parsedState{Nonce: nonce, CodeVerifier: verifier}, expiry, nil
}

func audienceIsExact(raw json.RawMessage, clientID string) bool {
	var one string
	if json.Unmarshal(raw, &one) == nil {
		return one == clientID
	}
	var many []string
	return json.Unmarshal(raw, &many) == nil && len(many) == 1 && many[0] == clientID
}

func randomURLToken(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(value), nil
}

func clonePayload(payload map[string]any) map[string]any {
	data, err := json.Marshal(payload)
	if err != nil {
		return map[string]any{}
	}
	var cloned map[string]any
	if json.Unmarshal(data, &cloned) != nil || cloned == nil {
		return map[string]any{}
	}
	return cloned
}

func payloadEqual(a, b map[string]any) bool { return jsonEqual(a, b) }
func safeMethod(method string) bool {
	return method == http.MethodGet || method == http.MethodHead || method == http.MethodOptions
}

func contains(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

type responseError struct {
	status  int
	message string
}

func (e *responseError) Error() string { return e.message }

func writeCommitError(w http.ResponseWriter, err error) {
	var response *responseError
	if errors.As(err, &response) {
		writeError(w, response.status, response.message)
		return
	}
	writeError(w, http.StatusInternalServerError, "authentication session unavailable")
}

func writeError(w http.ResponseWriter, code int, message string) {
	writeJSON(w, code, map[string]string{"detail": message})
}

func writeJSON(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}
