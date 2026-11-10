package auth

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestSecureCookieRoundTrip(t *testing.T) {
	const handle = "opaque-database-session-handle"
	signer := newCookieSigner("test-session-secret", 3600)
	cookie, err := signer.sign(handle)
	if err != nil {
		t.Fatalf("sign cookie: %v", err)
	}
	got, err := signer.unsign(cookie)
	if err != nil || got != handle {
		t.Fatalf("cookie verification = (%q, %v), want (%q, nil)", got, err, handle)
	}
}

func TestSecureCookieRejectsTampering(t *testing.T) {
	signer := newCookieSigner("secret", 3600)
	cookie, err := signer.sign("handle")
	if err != nil {
		t.Fatalf("sign cookie: %v", err)
	}
	if _, err := signer.unsign(cookie + "x"); err == nil {
		t.Fatal("accepted tampered cookie")
	}
}

func TestOIDCDiscoveryIsDeferredUntilLogin(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		http.Error(w, "provider unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	a, err := NewWithContext(context.Background(), Config{
		OIDC: &OIDCConfig{
			Issuer: server.URL, ClientID: "client", ClientSecret: "secret",
			SessionSecret: "session-secret", PublicBaseURL: "https://agentplane.test",
		},
		Database:   new(sql.DB),
		HTTPClient: server.Client(),
	})
	if err != nil {
		t.Fatalf("NewWithContext() error = %v", err)
	}
	if got := requests.Load(); got != 0 {
		t.Fatalf("OIDC discovery requests during construction = %d, want 0", got)
	}

	session := &RequestSession{pendingLogins: make(map[string]loginAttempt)}
	r := httptest.NewRequest(http.MethodGet, "/auth/login", nil)
	r = r.WithContext(context.WithValue(r.Context(), sessionContextKey{}, session))
	w := httptest.NewRecorder()
	a.login(w, r)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("login status = %d, want %d", w.Code, http.StatusServiceUnavailable)
	}
	if got := requests.Load(); got != 1 {
		t.Fatalf("OIDC discovery requests after login = %d, want 1", got)
	}
}

func TestCallbackRejectsInvalidLoginAttempt(t *testing.T) {
	valid := loginAttempt{Nonce: "nonce", CodeVerifier: "verifier", ExpiresAt: time.Now().UTC().Add(time.Minute)}
	for _, tc := range []struct {
		name     string
		attempts map[string]loginAttempt
	}{
		{name: "unknown state", attempts: map[string]loginAttempt{"different-state": valid}},
		{name: "expired", attempts: map[string]loginAttempt{"state": {Nonce: "nonce", CodeVerifier: "verifier", ExpiresAt: time.Now().UTC().Add(-time.Second)}}},
		{name: "missing nonce", attempts: map[string]loginAttempt{"state": {CodeVerifier: "verifier", ExpiresAt: valid.ExpiresAt}}},
		{name: "missing verifier", attempts: map[string]loginAttempt{"state": {Nonce: "nonce", ExpiresAt: valid.ExpiresAt}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := &RequestSession{pendingLogins: tc.attempts}
			r := httptest.NewRequest(http.MethodGet, "/auth/callback?state=state&code=code", nil)
			r = r.WithContext(context.WithValue(r.Context(), sessionContextKey{}, session))
			w := httptest.NewRecorder()
			new(Auth).callback(w, r)
			if w.Code != http.StatusUnauthorized {
				t.Fatalf("callback status = %d, want 401", w.Code)
			}
		})
	}
}

func TestTokenReviewRequiresAudienceAndAllowlistedSubject(t *testing.T) {
	reviewer := &fakeTokenReviews{review: &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}}
	caller, code, _ := reviewToken(context.Background(), reviewer, "agentplane", map[string]struct{}{"system:serviceaccount:tools:runner": {}}, "opaque")
	if code != 0 || caller == nil || caller.Kind != CallerToken {
		t.Fatalf("accepted TokenReview = (%+v, %d)", caller, code)
	}
	if len(reviewer.last.Spec.Audiences) != 1 || reviewer.last.Spec.Audiences[0] != "agentplane" || reviewer.last.Spec.Token != "opaque" {
		t.Fatalf("TokenReview request = %+v", reviewer.last.Spec)
	}

	reviewer.review.Status.Audiences = []string{"elsewhere"}
	if caller, code, _ := reviewToken(context.Background(), reviewer, "agentplane", map[string]struct{}{"system:serviceaccount:tools:runner": {}}, "opaque"); caller != nil || code != http.StatusUnauthorized {
		t.Fatalf("wrong-audience review = (%+v, %d)", caller, code)
	}
	reviewer.review.Status.Audiences = []string{"agentplane"}
	if caller, code, _ := reviewToken(context.Background(), reviewer, "agentplane", map[string]struct{}{}, "opaque"); caller != nil || code != http.StatusForbidden {
		t.Fatalf("disallowed subject = (%+v, %d)", caller, code)
	}
	reviewer.err = errors.New("unavailable")
	if caller, code, _ := reviewToken(context.Background(), reviewer, "agentplane", map[string]struct{}{}, "opaque"); caller != nil || code != http.StatusServiceUnavailable {
		t.Fatalf("review failure = (%+v, %d)", caller, code)
	}
}

func TestRequireCallerContextAndSameOrigin(t *testing.T) {
	reviewer := &fakeTokenReviews{review: &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}}
	a := &Auth{cfg: Config{TokenReviews: reviewer, TokenAudience: "agentplane"}, subjects: map[string]struct{}{"system:serviceaccount:tools:runner": {}}}
	called := false
	next := a.RequireCaller(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		caller, ok := CallerFromContext(r.Context())
		if !ok || caller.Principal != "system:serviceaccount:tools:runner" {
			t.Errorf("caller from context = (%+v, %v)", caller, ok)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	r := httptest.NewRequest(http.MethodPost, "/protected", nil)
	r.Header.Set("Authorization", "Bearer opaque")
	w := httptest.NewRecorder()
	next.ServeHTTP(w, r)
	if !called || w.Code != http.StatusNoContent {
		t.Fatalf("token request: called=%v status=%d", called, w.Code)
	}

	called = false
	r = httptest.NewRequest(http.MethodPost, "/protected", nil)
	w = httptest.NewRecorder()
	next.ServeHTTP(w, r)
	if called || w.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous request: called=%v status=%d", called, w.Code)
	}
}

func TestOperatorCallerRequiresSameOriginForUnsafeMethods(t *testing.T) {
	a := &Auth{cfg: Config{OIDC: &OIDCConfig{PublicBaseURL: "https://agentplane.test"}}}
	call := func(method, origin string) *httptest.ResponseRecorder {
		next := a.RequireCaller(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			caller, ok := CallerFromContext(r.Context())
			if !ok || caller.Kind != CallerOperator || caller.Principal != "operator" {
				t.Errorf("caller from context = (%+v, %v)", caller, ok)
			}
			w.WriteHeader(http.StatusNoContent)
		}))
		r := httptest.NewRequest(method, "/protected", nil)
		if origin != "" {
			r.Header.Set("Origin", origin)
		}
		r = r.WithContext(context.WithValue(r.Context(), sessionContextKey{}, &RequestSession{Login: &OperatorSession{Username: "operator"}}))
		w := httptest.NewRecorder()
		next.ServeHTTP(w, r)
		return w
	}
	if response := call(http.MethodPost, "https://agentplane.test"); response.Code != http.StatusNoContent {
		t.Fatalf("same-origin POST status = %d", response.Code)
	}
	if response := call(http.MethodPost, "https://attacker.test"); response.Code != http.StatusForbidden {
		t.Fatalf("cross-origin POST status = %d", response.Code)
	}
	if response := call(http.MethodGet, ""); response.Code != http.StatusNoContent {
		t.Fatalf("GET without Origin status = %d", response.Code)
	}
}

func TestLoginAttemptMergePreservesConcurrentKeys(t *testing.T) {
	before := map[string]loginAttempt{"a": {Nonce: "old-a"}, "b": {Nonce: "old-b"}}
	concurrent := map[string]loginAttempt{"a": {Nonce: "new-a"}, "b": {Nonce: "old-b"}, "other": {Nonce: "other"}}
	after := map[string]loginAttempt{"a": {Nonce: "old-a"}, "b": {Nonce: "new-b"}}
	merged := mergeLoginAttempts(concurrent, before, after)
	if merged["a"].Nonce != "new-a" || merged["b"].Nonce != "new-b" || merged["other"].Nonce != "other" {
		t.Fatalf("merge result = %#v", merged)
	}
}

func TestAudienceMustBeTheSingleConfiguredClient(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want bool
	}{
		{`"client"`, true},
		{`["client"]`, true},
		{`["client","other"]`, false},
		{`"other"`, false},
		{`null`, false},
	} {
		if got := audienceIsExact(json.RawMessage(tc.raw), "client"); got != tc.want {
			t.Errorf("audienceIsExact(%s) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

type fakeTokenReviews struct {
	review *authenticationv1.TokenReview
	err    error
	last   *authenticationv1.TokenReview
}

func (f *fakeTokenReviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	f.last = review
	return f.review, f.err
}

var _ = errors.New

func TestTokenReviewRejectionLogsOnlyCuratedIdentity(t *testing.T) {
	const token = "opaque-bearer-must-not-be-logged"
	const subject = "system:serviceaccount:testing:caller"
	for _, reason := range []string{"unauthenticated", "missing_username", "wrong_audience", "subject_not_allowed"} {
		t.Run(reason, func(t *testing.T) {
			review := &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
				Authenticated: true,
				User:          authenticationv1.UserInfo{Username: subject}, Audiences: []string{"app-audience"}, Error: "private-provider-detail: " + token,
			}}
			switch reason {
			case "unauthenticated":
				review.Status.Authenticated = false
			case "missing_username":
				review.Status.User.Username = ""
			case "wrong_audience":
				review.Status.Audiences = []string{"wrong"}
			}
			var log bytes.Buffer
			original := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&log, nil)))
			defer slog.SetDefault(original)
			caller, code, detail := reviewToken(t.Context(), &fakeTokenReviews{review: review}, "app-audience", map[string]struct{}{}, token)
			if caller != nil || (code != http.StatusUnauthorized && code != http.StatusForbidden) {
				t.Fatalf("rejection = %v %d %s", caller, code, detail)
			}
			var record map[string]any
			if err := json.Unmarshal(log.Bytes(), &record); err != nil {
				t.Fatal(err)
			}
			if record["reason"] != reason {
				t.Fatalf("log = %s", log.String())
			}
			if strings.Contains(log.String()+detail, token) || strings.Contains(log.String()+detail, "private-provider-detail") {
				t.Fatal("credential-bearing TokenReview data escaped")
			}
		})
	}
}
