package gateway

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"golang.org/x/oauth2"
)

func TestOIDCOwnerFlowVerifiesPKCENonceAndSubject(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	var server *httptest.Server
	var nonce, challenge string
	subject := "owner"
	server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		issuer := server.URL + "/"
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_ = json.NewEncoder(w).Encode(map[string]any{"issuer": issuer, "authorization_endpoint": server.URL + "/authorize", "token_endpoint": server.URL + "/token", "jwks_uri": server.URL + "/jwks", "id_token_signing_alg_values_supported": []string{"RS256"}})
		case "/jwks":
			_ = json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "key", Algorithm: string(jose.RS256), Use: "sig"}}})
		case "/token":
			_ = r.ParseForm()
			if r.Form.Get("code") != "synthetic" || oauth2.S256ChallengeFromVerifier(r.Form.Get("code_verifier")) != challenge {
				http.Error(w, "invalid grant", 400)
				return
			}
			signer, e := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithHeader("kid", "key"))
			if e != nil {
				t.Error(e)
				return
			}
			raw, e := jwt.Signed(signer).Claims(map[string]any{"iss": issuer, "sub": subject, "aud": "client", "exp": time.Now().Add(time.Hour).Unix(), "iat": time.Now().Unix(), "nonce": nonce, "preferred_username": "user"}).Serialize()
			if e != nil {
				t.Error(e)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "synthetic", "token_type": "Bearer", "id_token": raw})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	cfg := testOwnerConfig()
	cfg.Issuer = server.URL + "/"
	owner, err := newOwnerAuth(cfg)
	if err != nil {
		t.Fatal(err)
	}
	begin := func() (*http.Cookie, string) {
		req := httptest.NewRequest("GET", "https://sessions.invalid/auth/login", nil)
		req = req.WithContext(oidc.ClientContext(req.Context(), server.Client()))
		out := httptest.NewRecorder()
		owner.login(out, req)
		if out.Code != http.StatusFound {
			t.Fatalf("login: %d %s", out.Code, out.Body)
		}
		loc, err := url.Parse(out.Header().Get("Location"))
		if err != nil {
			t.Fatal(err)
		}
		query := loc.Query()
		nonce = query.Get("nonce")
		challenge = query.Get("code_challenge")
		if nonce == "" || challenge == "" || query.Get("code_challenge_method") != "S256" {
			t.Fatal("missing OIDC PKCE or nonce")
		}
		return out.Result().Cookies()[0], query.Get("state")
	}
	cookie, state := begin()
	request := httptest.NewRequest("GET", "https://sessions.invalid/auth/callback?code=synthetic&state="+url.QueryEscape(state), nil)
	request.AddCookie(cookie)
	request = request.WithContext(oidc.ClientContext(request.Context(), server.Client()))
	result := httptest.NewRecorder()
	owner.callback(result, request)
	if result.Code != http.StatusSeeOther {
		t.Fatalf("callback: %d %s", result.Code, result.Body)
	}
	cookies := result.Result().Cookies()
	var session *http.Cookie
	for _, c := range cookies {
		if c.Name == ownerCookie {
			session = c
		}
	}
	if session == nil || !session.HttpOnly || !session.Secure || strings.Contains(session.Value, "synthetic") {
		t.Fatal("unsafe or missing owner cookie")
	}
	browser := httptest.NewRequest("GET", "https://sessions.invalid/", nil)
	browser.AddCookie(session)
	if !owner.loggedIn(browser) {
		t.Fatal("valid owner not signed in")
	}
	subject = "someone-else"
	cookie, state = begin()
	request = httptest.NewRequest("GET", "https://sessions.invalid/auth/callback?code=synthetic&state="+url.QueryEscape(state), nil)
	request.AddCookie(cookie)
	request = request.WithContext(oidc.ClientContext(request.Context(), server.Client()))
	result = httptest.NewRecorder()
	owner.callback(result, request)
	if result.Code != http.StatusUnauthorized {
		t.Fatalf("wrong subject: %d", result.Code)
	}
}
