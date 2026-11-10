package gateway

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/gorilla/securecookie"
	"golang.org/x/oauth2"
)

// OwnerConfig authenticates the single browser owner using OIDC authorization
// code with PKCE. Browser cookies contain no upstream tokens.
type OwnerConfig struct {
	Issuer, ClientID, ClientSecret, SessionSecret, PublicBaseURL, AllowedSubject string
}
type ownerSession struct {
	Issuer, Subject, Username string
	ExpiresAt                 int64
}
type (
	pendingLogin struct{ State, Nonce, Verifier string }
	ownerAuth    struct {
		cfg         OwnerConfig
		cookie      *securecookie.SecureCookie
		loginCookie *securecookie.SecureCookie
		mu          sync.Mutex
		provider    *oidc.Provider
		oauth       *oauth2.Config
	}
)

func newOwnerAuth(cfg OwnerConfig) (*ownerAuth, error) {
	issuer, err := url.Parse(cfg.Issuer)
	if err != nil || issuer.Scheme != "https" || issuer.Host == "" || issuer.User != nil || issuer.RawQuery != "" || issuer.Fragment != "" {
		return nil, errors.New("invalid OIDC issuer")
	}
	base, err := url.Parse(cfg.PublicBaseURL)
	if err != nil || base.Scheme != "https" || base.Host == "" || base.User != nil || (base.Path != "" && base.Path != "/") || base.RawQuery != "" || base.Fragment != "" {
		return nil, errors.New("invalid public origin")
	}
	if cfg.ClientID == "" || len(cfg.ClientSecret) < 32 || len(cfg.SessionSecret) < 32 || cfg.AllowedSubject == "" {
		return nil, errors.New("invalid owner configuration")
	}
	cfg.PublicBaseURL = strings.TrimSuffix(cfg.PublicBaseURL, "/")
	cfg.Issuer = strings.TrimRight(cfg.Issuer, "/") + "/"
	cookie := securecookie.New([]byte(cfg.SessionSecret), nil)
	cookie.MaxAge(3600)
	login := securecookie.New([]byte(cfg.SessionSecret), nil)
	login.MaxAge(600)
	return &ownerAuth{cfg: cfg, cookie: cookie, loginCookie: login}, nil
}

func (a *ownerAuth) oidc(ctx context.Context) (*oidc.Provider, *oauth2.Config, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.provider != nil {
		return a.provider, a.oauth, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	provider, err := oidc.NewProvider(ctx, a.cfg.Issuer)
	if err != nil {
		return nil, nil, err
	}
	oauth := &oauth2.Config{ClientID: a.cfg.ClientID, ClientSecret: a.cfg.ClientSecret, Endpoint: provider.Endpoint(), RedirectURL: a.cfg.PublicBaseURL + "/auth/callback", Scopes: []string{oidc.ScopeOpenID, "profile", "email"}}
	a.provider = provider
	a.oauth = oauth
	return provider, oauth, nil
}

func randomToken() (string, error) {
	var b [32]byte
	_, err := rand.Read(b[:])
	return base64.RawURLEncoding.EncodeToString(b[:]), err
}

func writeCookie(w http.ResponseWriter, name, value string, maxAge int) {
	http.SetCookie(w, &http.Cookie{Name: name, Value: value, Path: "/", HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode, MaxAge: maxAge})
}

const (
	ownerCookie = "__Host-platform-sessions"
	loginCookie = "__Host-platform-sessions-login"
)

func (a *ownerAuth) loggedIn(r *http.Request) bool {
	cookie, err := r.Cookie(ownerCookie)
	if err != nil {
		return false
	}
	var value ownerSession
	if a.cookie.Decode(ownerCookie, cookie.Value, &value) != nil {
		return false
	}
	return value.Issuer == a.cfg.Issuer && value.Subject == a.cfg.AllowedSubject && value.Username != "" && value.ExpiresAt > time.Now().Unix()
}

func (a *ownerAuth) login(w http.ResponseWriter, r *http.Request) {
	_, oauth, err := a.oidc(r.Context())
	if err != nil {
		http.Error(w, "identity provider unavailable", http.StatusServiceUnavailable)
		return
	}
	state, err := randomToken()
	if err != nil {
		http.Error(w, "login unavailable", 500)
		return
	}
	nonce, err := randomToken()
	if err != nil {
		http.Error(w, "login unavailable", 500)
		return
	}
	pending := pendingLogin{State: state, Nonce: nonce, Verifier: oauth2.GenerateVerifier()}
	encoded, err := a.loginCookie.Encode(loginCookie, pending)
	if err != nil {
		http.Error(w, "login unavailable", 500)
		return
	}
	writeCookie(w, loginCookie, encoded, 600)
	http.Redirect(w, r, oauth.AuthCodeURL(state, oauth2.S256ChallengeOption(pending.Verifier), oauth2.SetAuthURLParam("nonce", nonce)), http.StatusFound)
}

func (a *ownerAuth) callback(w http.ResponseWriter, r *http.Request) {
	fail := func() { http.Error(w, "sign-in failed", http.StatusUnauthorized) }
	cookie, err := r.Cookie(loginCookie)
	writeCookie(w, loginCookie, "", -1)
	if err != nil {
		fail()
		return
	}
	var pending pendingLogin
	if a.loginCookie.Decode(loginCookie, cookie.Value, &pending) != nil || pending.State == "" || pending.State != r.URL.Query().Get("state") || r.URL.Query().Get("code") == "" {
		fail()
		return
	}
	provider, oauth, err := a.oidc(r.Context())
	if err != nil {
		http.Error(w, "identity provider unavailable", http.StatusServiceUnavailable)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	token, err := oauth.Exchange(ctx, r.URL.Query().Get("code"), oauth2.VerifierOption(pending.Verifier))
	if err != nil {
		fail()
		return
	}
	raw, ok := token.Extra("id_token").(string)
	if !ok || raw == "" {
		fail()
		return
	}
	id, err := provider.Verifier(&oidc.Config{ClientID: a.cfg.ClientID}).Verify(ctx, raw)
	if err != nil || id.Nonce != pending.Nonce {
		fail()
		return
	}
	var claims struct {
		Subject         string          `json:"sub"`
		Username        string          `json:"preferred_username"`
		AuthorizedParty string          `json:"azp"`
		Audience        json.RawMessage `json:"aud"`
	}
	if id.Claims(&claims) != nil || claims.Subject != a.cfg.AllowedSubject || claims.Username == "" || (claims.AuthorizedParty != "" && claims.AuthorizedParty != a.cfg.ClientID) {
		fail()
		return
	}
	var audiences []string
	var single string
	if json.Unmarshal(claims.Audience, &single) == nil {
		audiences = []string{single}
	} else if json.Unmarshal(claims.Audience, &audiences) != nil {
		fail()
		return
	}
	if len(audiences) > 1 && claims.AuthorizedParty != a.cfg.ClientID {
		fail()
		return
	}
	expiry := time.Now().Add(time.Hour)
	if id.Expiry.Before(expiry) {
		expiry = id.Expiry
	}
	encoded, err := a.cookie.Encode(ownerCookie, ownerSession{Issuer: a.cfg.Issuer, Subject: claims.Subject, Username: claims.Username, ExpiresAt: expiry.Unix()})
	if err != nil {
		http.Error(w, "login unavailable", 500)
		return
	}
	writeCookie(w, ownerCookie, encoded, int(time.Until(expiry).Seconds()))
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func (a *ownerAuth) logout(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != a.cfg.PublicBaseURL {
		http.Error(w, "invalid origin", http.StatusForbidden)
		return
	}
	writeCookie(w, ownerCookie, "", -1)
	http.Redirect(w, r, "/auth/signed-out", http.StatusSeeOther)
}
