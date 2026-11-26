package auth

import (
	"context"
	"crypto"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth/session"
	"github.com/agentydragon/ducktape/agentplane/app/internal/httpresponse"
	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

type Auth struct {
	cfg     Config
	oidc    *openID
	oidcMu  sync.Mutex
	pinned  *oidc.IDTokenVerifier
	store   *session.Store
	signer  *cookieSigner
	changes ChangeSource
}

// New constructs the auth boundary. OIDC discovery is deferred until a browser uses the login route.
func New(cfg Config) (*Auth, error) {
	a := &Auth{cfg: cfg, changes: cfg.SessionChanges}
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
	if settings.StaticKeys != nil {
		keys := make([]crypto.PublicKey, len(settings.StaticKeys))
		for index, key := range settings.StaticKeys {
			keys[index] = key
		}
		a.pinned = oidc.NewVerifier(settings.Issuer, &oidc.StaticKeySet{PublicKeys: keys}, &oidc.Config{
			ClientID: settings.ClientID, SupportedSigningAlgs: []string{oidc.EdDSA},
		})
	}
	a.store = session.NewStore(
		cfg.Database, time.Duration(settings.SessionIdleSeconds)*time.Second,
		time.Duration(settings.SessionActivityStepSeconds)*time.Second,
	)
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
	provider, err := oidc.NewProvider(providerCtx, settings.Issuer)
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
		traceAuth(r, "admitted")
		requestSession, cookie, row, initial, err := a.loadSession(r)
		if err != nil {
			traceAuth(r, "load_session_failed")
			http.Error(w, "authentication session unavailable", http.StatusInternalServerError)
			return
		}
		traceAuth(r, "session_loaded")
		r = r.WithContext(context.WithValue(r.Context(), sessionContextKey{}, requestSession))
		wrapped := &sessionResponseWriter{ResponseWriter: w, commit: func() error {
			traceAuth(r, "commit_started")
			err := a.commitSession(r, w, requestSession, cookie, row, initial)
			traceAuth(r, "commit_finished")
			return err
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
		traceAuth(r, "handler_finished")
	})
}

// traceAuth records only explicitly traced fixture logins, without query strings or credentials.
func traceAuth(r *http.Request, phase string) {
	id := r.Header.Get("X-Agentplane-Auth-Trace")
	if len(id) != 32 {
		return
	}
	for _, digit := range id {
		if !strings.ContainsRune("0123456789abcdef", digit) {
			return
		}
	}
	slog.InfoContext(r.Context(), "auth login phase", "trace_id", id, "path", r.URL.Path, "phase", phase)
}

// RequireCaller protects a route with an operator cookie or an operator's OIDC ID token.
func (a *Auth) RequireCaller(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		caller, status, message := a.caller(r)
		if caller == nil {
			httpresponse.WriteDetailError(w, status, message)
			return
		}
		// Only the ambient cookie needs a same-origin check; a bearer is never sent by the browser.
		if session, ok := SessionFromContext(r.Context()); ok && session.Login != nil && !safeMethod(r.Method) {
			expected := strings.TrimRight(a.cfg.OIDC.PublicBaseURL, "/")
			if r.Header.Get("Origin") != expected {
				httpresponse.WriteDetailError(w, http.StatusForbidden, "cross-origin request")
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
	return a.store.UntilTerminated(ctx, session.ID(rowID), a.changes)
}

type CallerKind string

const (
	CallerOperator CallerKind = "operator"
)

type Caller struct {
	Issuer    string
	Subject   string
	Kind      CallerKind
	Principal string
}

type (
	callerContextKey  struct{}
	sessionContextKey struct{}
)

type RequestSession struct {
	pendingLogins map[string]loginAttempt
	Login         *OperatorSession
	Row           *SessionRow
	replace       *OperatorSession
	logout        bool
}

// LogIn replaces this pending login's session with a fresh, logged-in row.
func (s *RequestSession) LogIn(login OperatorSession) {
	s.pendingLogins = map[string]loginAttempt{}
	s.replace = &login
	s.logout = false
}

// End clears the browser session at response commit.
func (s *RequestSession) End() {
	s.pendingLogins = map[string]loginAttempt{}
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

func (a *Auth) loadSession(r *http.Request) (*RequestSession, *http.Cookie, *session.Record, map[string]loginAttempt, error) {
	initial := map[string]loginAttempt{}
	var cookie *http.Cookie
	var row *session.Record
	if parsed, err := r.Cookie(a.cfg.OIDC.CookieName()); err == nil {
		cookie = parsed
		if handle, err := a.signer.unsign(parsed.Value); err == nil {
			row, err = a.store.Read(r.Context(), session.IDForHandle(handle))
			if err != nil {
				return nil, cookie, nil, nil, err
			}
		}
	}
	if row != nil {
		initial = operatorAttempts(row.PendingLogins)
	}
	session := &RequestSession{pendingLogins: maps.Clone(initial)}
	if row != nil {
		session.Login = operatorSession(row.Login)
		session.Row = &SessionRow{store: a.store, id: row.ID}
	}
	return session, cookie, row, initial, nil
}

func (a *Auth) commitSession(r *http.Request, w http.ResponseWriter, requestSession *RequestSession, cookie *http.Cookie, row *session.Record, initial map[string]loginAttempt) error {
	if requestSession.logout {
		if row != nil {
			if err := a.store.Terminate(r.Context(), row.ID); err != nil {
				return err
			}
		}
		a.forgetCookie(w)
		w.Header().Set("Clear-Site-Data", `"storage:agentplane-history"`)
	} else if requestSession.replace != nil {
		absolute := time.Now().UTC().Add(time.Duration(a.cfg.OIDC.SessionMaxSeconds) * time.Second)
		var pending session.ID
		if row != nil {
			pending = row.ID
		}
		handle, inserted, err := a.store.CompleteLogin(r.Context(), pending, persistedAttempts(requestSession.pendingLogins), *persistedLogin(requestSession.replace), absolute)
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
	} else if !maps.Equal(initial, requestSession.pendingLogins) {
		if row == nil {
			pendingTTL := pendingLoginTTL
			maxAge := time.Duration(a.cfg.OIDC.SessionMaxSeconds) * time.Second
			if pendingTTL > maxAge {
				pendingTTL = maxAge
			}
			absolute := time.Now().UTC().Add(pendingTTL)
			handle, err := a.store.Create(r.Context(), persistedAttempts(requestSession.pendingLogins), absolute)
			if err != nil {
				return err
			}
			if err := a.rememberCookie(w, handle, absolute); err != nil {
				return err
			}
		} else {
			saved, err := a.store.SavePending(r.Context(), row.ID, persistedAttempts(initial), persistedAttempts(requestSession.pendingLogins))
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
	traceAuth(r, "login_started")
	session, ok := SessionFromContext(r.Context())
	if !ok {
		httpresponse.WriteDetailError(w, http.StatusInternalServerError, "session middleware is required")
		return
	}
	traceAuth(r, "discovery_started")
	provider, err := a.openIDClient(r.Context())
	traceAuth(r, "discovery_finished")
	if err != nil {
		httpresponse.WriteDetailError(w, http.StatusServiceUnavailable, "identity provider unavailable")
		return
	}
	state, err := randomURLToken(32)
	if err != nil {
		httpresponse.WriteDetailError(w, http.StatusInternalServerError, "could not start login")
		return
	}
	nonce, err := randomURLToken(32)
	if err != nil {
		httpresponse.WriteDetailError(w, http.StatusInternalServerError, "could not start login")
		return
	}
	verifier := oauth2.GenerateVerifier()
	challenge := oauth2.S256ChallengeFromVerifier(verifier)
	url := provider.oauth.AuthCodeURL(state, oauth2.AccessTypeOffline, oauth2.SetAuthURLParam("code_challenge", challenge), oauth2.SetAuthURLParam("code_challenge_method", "S256"), oauth2.SetAuthURLParam("nonce", nonce))
	session.pendingLogins[state] = loginAttempt{
		Nonce: nonce, CodeVerifier: verifier, ExpiresAt: time.Now().UTC().Add(pendingLoginTTL),
	}
	http.Redirect(w, r, url, http.StatusFound)
}

func (a *Auth) callback(w http.ResponseWriter, r *http.Request) {
	traceAuth(r, "callback_started")
	if r.URL.Query().Get("error") != "" {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	state := r.URL.Query().Get("state")
	session, ok := SessionFromContext(r.Context())
	if !ok || state == "" {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC state is missing")
		return
	}
	data, exists := session.pendingLogins[state]
	if !exists || data.Nonce == "" || data.CodeVerifier == "" || !data.ExpiresAt.After(time.Now()) {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC state is invalid or expired")
		return
	}
	code := r.URL.Query().Get("code")
	if code == "" {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC authorization code is missing")
		return
	}
	provider, err := a.openIDClient(r.Context())
	if err != nil {
		httpresponse.WriteDetailError(w, http.StatusServiceUnavailable, "identity provider unavailable")
		return
	}
	delete(session.pendingLogins, state)
	ctx := r.Context()
	if a.cfg.HTTPClient != nil {
		ctx = oidc.ClientContext(ctx, a.cfg.HTTPClient)
	}
	traceAuth(r, "exchange_started")
	token, err := provider.oauth.Exchange(ctx, code, oauth2.SetAuthURLParam("code_verifier", data.CodeVerifier))
	traceAuth(r, "exchange_finished")
	if err != nil {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	if token.AccessToken == "" {
		httpresponse.WriteDetailError(w, http.StatusBadGateway, "identity provider returned an invalid response")
		return
	}
	rawIDToken, ok := token.Extra("id_token").(string)
	if !ok || rawIDToken == "" {
		httpresponse.WriteDetailError(w, http.StatusBadGateway, "identity provider returned an invalid response")
		return
	}
	idToken, err := provider.verifier.Verify(ctx, rawIDToken)
	if err != nil || idToken.Nonce != data.Nonce {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC authentication failed")
		return
	}
	login, ok := a.operatorIdentity(idToken)
	if !ok {
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "OIDC identity is not accepted")
		return
	}
	if a.cfg.StoreFederationTokens && token.AccessToken != "" && !token.Expiry.IsZero() && token.Expiry.After(time.Now()) {
		login.Tokens = &LoginTokens{AccessToken: token.AccessToken, ExpiresAt: token.Expiry}
		if refresh, ok := token.Extra("refresh_token").(string); ok {
			login.Tokens.RefreshToken = refresh
		}
	}
	session.LogIn(login)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// operatorIdentity applies the app's checks beyond go-oidc's to a verified ID token.
func (a *Auth) operatorIdentity(idToken *oidc.IDToken) (OperatorSession, bool) {
	claims := struct {
		Issuer          string          `json:"iss"`
		Subject         string          `json:"sub"`
		Username        string          `json:"preferred_username"`
		AuthorizedParty string          `json:"azp"`
		Audience        json.RawMessage `json:"aud"`
	}{}
	if err := idToken.Claims(&claims); err != nil {
		return OperatorSession{}, false
	}
	if claims.Issuer != a.cfg.OIDC.Issuer || claims.Subject == "" || claims.Username == "" || (claims.AuthorizedParty != "" && claims.AuthorizedParty != a.cfg.OIDC.ClientID) || !audienceIsExact(claims.Audience, a.cfg.OIDC.ClientID) || !idToken.Expiry.After(time.Now()) {
		return OperatorSession{}, false
	}
	return OperatorSession{Issuer: claims.Issuer, Subject: claims.Subject, Username: claims.Username}, true
}

func (a *Auth) logout(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != strings.TrimRight(a.cfg.OIDC.PublicBaseURL, "/") {
		httpresponse.WriteDetailError(w, http.StatusForbidden, "logout requires exact same-origin Origin")
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
		httpresponse.WriteDetailError(w, http.StatusUnauthorized, "not logged in")
		return
	}
	httpresponse.WriteJSON(w, http.StatusOK, map[string]string{"username": session.Login.Username})
}

func (a *Auth) caller(r *http.Request) (*Caller, int, string) {
	if session, ok := SessionFromContext(r.Context()); ok && session.Login != nil {
		return &Caller{Kind: CallerOperator, Principal: session.Login.Username, Issuer: session.Login.Issuer, Subject: session.Login.Subject}, 0, ""
	}
	fields := strings.Fields(r.Header.Get("Authorization"))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "Bearer") {
		return nil, http.StatusUnauthorized, "no session and no accepted token"
	}
	if a.cfg.OIDC != nil && unverifiedIssuer(fields[1]) == a.cfg.OIDC.Issuer {
		return a.idTokenCaller(r.Context(), fields[1])
	}
	return nil, http.StatusUnauthorized, "no session and no accepted token"
}

// idTokenCaller accepts an operator's ID token as a cookie session's operator, with no session row.
func (a *Auth) idTokenCaller(ctx context.Context, raw string) (*Caller, int, string) {
	verifier := a.pinned
	if verifier == nil {
		provider, err := a.openIDClient(ctx)
		if err != nil {
			return nil, http.StatusServiceUnavailable, "identity provider unavailable"
		}
		verifier = provider.verifier
		if a.cfg.HTTPClient != nil {
			ctx = oidc.ClientContext(ctx, a.cfg.HTTPClient)
		}
	}
	idToken, err := verifier.Verify(ctx, raw)
	if err != nil {
		return nil, http.StatusUnauthorized, "no session and no accepted token"
	}
	login, ok := a.operatorIdentity(idToken)
	if !ok {
		return nil, http.StatusUnauthorized, "OIDC identity is not accepted"
	}
	return &Caller{Kind: CallerOperator, Principal: login.Username, Issuer: login.Issuer, Subject: login.Subject}, 0, ""
}

// unverifiedIssuer reads a JWT's `iss` only to pick its verifier, which checks it; "" if unreadable.
func unverifiedIssuer(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return ""
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return ""
	}
	var claims struct {
		Issuer string `json:"iss"`
	}
	if json.Unmarshal(payload, &claims) != nil {
		return ""
	}
	return claims.Issuer
}

// loginAttempt is the app's server-side state for one authorization-code flow.
type loginAttempt struct {
	Nonce        string    `json:"nonce"`
	CodeVerifier string    `json:"code_verifier"`
	ExpiresAt    time.Time `json:"expires_at"`
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

func safeMethod(method string) bool {
	return method == http.MethodGet || method == http.MethodHead || method == http.MethodOptions
}

type responseError struct {
	status  int
	message string
}

func (e *responseError) Error() string { return e.message }

func writeCommitError(w http.ResponseWriter, err error) {
	var response *responseError
	if errors.As(err, &response) {
		httpresponse.WriteDetailError(w, response.status, response.message)
		return
	}
	httpresponse.WriteDetailError(w, http.StatusInternalServerError, "authentication session unavailable")
}
