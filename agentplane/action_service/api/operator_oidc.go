package api

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

// TokenProfile is the issuer's access-token contract, which decides how the `azp` claim is checked.
type TokenProfile string

const (
	// ProfileAuthentik tokens name the audience in `azp` too.
	ProfileAuthentik TokenProfile = "authentik"
	// ProfileDex tokens leave `azp` out for a single audience; one that is present must still name it.
	ProfileDex TokenProfile = "dex"
)

// OperatorOIDCSettings pins whose tokens prove an operator: the keys of the Python service's `operator_oidc`
// (operator_oidc_settings.py), which are reviewed configuration, never read from a presented token.
type OperatorOIDCSettings struct {
	// Issuer is compared with a token's `iss` exactly, unnormalized (RFC 7519 section 4.1.1).
	Issuer       string       `mapstructure:"issuer"`
	Audience     string       `mapstructure:"audience"`
	JWKSURI      string       `mapstructure:"jwks_uri"`
	TokenProfile TokenProfile `mapstructure:"token_profile"`
}

func DefaultOperatorOIDCSettings() OperatorOIDCSettings {
	return OperatorOIDCSettings{TokenProfile: ProfileAuthentik}
}

// Validate refuses settings that cannot verify a token. Its errors name the key, never the value.
func (s OperatorOIDCSettings) Validate() error {
	if !endpoint(s.Issuer) {
		return errors.New("operator_oidc.issuer must be an https URL, or http on a loopback host, without credentials or fragment")
	}
	if strings.TrimSpace(s.Audience) == "" {
		return errors.New("operator_oidc.audience must not be blank")
	}
	if !endpoint(s.JWKSURI) {
		return errors.New("operator_oidc.jwks_uri must be an https URL, or http on a loopback host, without credentials or fragment")
	}
	if s.TokenProfile != ProfileAuthentik && s.TokenProfile != ProfileDex {
		return errors.New("operator_oidc.token_profile must be authentik or dex")
	}
	return nil
}

// endpoint is whether raw is an absolute URL the service may fetch keys from or compare an issuer with: https, or
// http on a loopback host, with a host and no credentials or fragment.
func endpoint(raw string) bool {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host == "" || parsed.User != nil || strings.Contains(raw, "#") {
		return false
	}
	switch parsed.Scheme {
	case "https":
		return true
	case "http":
		ip := net.ParseIP(parsed.Hostname())
		return parsed.Hostname() == "localhost" || ip != nil && ip.IsLoopback()
	default:
		return false
	}
}

const (
	// clockSkew is how far past its `exp` a token is still accepted.
	clockSkew = 30 * time.Second
	// keysLifetime bounds how long fetched signing keys are trusted: a key the issuer withdraws stops verifying
	// within it.
	keysLifetime = 5 * time.Minute
	// keysTimeout bounds one fetch of the signing keys.
	keysTimeout = 10 * time.Second
)

// OperatorVerifier verifies the single-audience RS256 access tokens an operator's issuer signs. go-oidc checks the
// signature, issuer, audience and expiry; the verifier adds the rules of the Python service's
// `util/oidc_principal.py` the library leaves out: one audience, a subject, and the profile's `azp`.
type OperatorVerifier struct {
	settings OperatorOIDCSettings
	verifier *oidc.IDTokenVerifier
}

func NewOperatorVerifier(settings OperatorOIDCSettings) (*OperatorVerifier, error) {
	if err := settings.Validate(); err != nil {
		return nil, err
	}
	// The keys are fetched without following a redirect, so only the pinned URL is ever asked.
	client := &http.Client{
		Timeout:       keysTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	keys := &expiringKeys{ctx: oidc.ClientContext(context.Background(), client), url: settings.JWKSURI}
	return &OperatorVerifier{settings: settings, verifier: oidc.NewVerifier(settings.Issuer, keys, &oidc.Config{
		ClientID:             settings.Audience,
		SupportedSigningAlgs: []string{oidc.RS256},
		// The library allows no skew on `exp`, so its clock runs behind by it.
		Now: func() time.Time { return time.Now().Add(-clockSkew) },
	})}, nil
}

// issued is whether the token's unverified `iss` claim names this verifier's issuer.
func (v *OperatorVerifier) issued(token string) bool {
	parsed, err := jwt.ParseSigned(token, []jose.SignatureAlgorithm{jose.RS256})
	var claims jwt.Claims
	return err == nil && parsed.UnsafeClaimsWithoutVerification(&claims) == nil && claims.Issuer == v.settings.Issuer
}

// Verify returns the operator a token proves.
func (v *OperatorVerifier) Verify(ctx context.Context, token string) (store.OperatorPrincipal, error) {
	verified, err := v.verifier.Verify(ctx, token)
	if err != nil {
		return store.OperatorPrincipal{}, err
	}
	var claims struct {
		AuthorizedParty *string `json:"azp"`
	}
	if err := verified.Claims(&claims); err != nil {
		return store.OperatorPrincipal{}, err
	}
	switch {
	case len(verified.Audience) != 1:
		return store.OperatorPrincipal{}, errors.New("operator token names more than its one audience")
	case strings.TrimSpace(verified.Subject) == "":
		return store.OperatorPrincipal{}, errors.New("operator token has no subject")
	case claims.AuthorizedParty != nil && *claims.AuthorizedParty != v.settings.Audience:
		return store.OperatorPrincipal{}, errors.New("operator token was authorized for another party")
	case claims.AuthorizedParty == nil && v.settings.TokenProfile == ProfileAuthentik:
		return store.OperatorPrincipal{}, errors.New("operator token names no authorized party")
	}
	return store.OperatorPrincipal{Issuer: v.settings.Issuer, Subject: verified.Subject}, nil
}

// expiringKeys is the issuer's signing keys, fetched again once they are keysLifetime old. go-oidc's
// RemoteKeySet keeps a key until a token it cannot verify makes it fetch again, so a withdrawn key would verify
// for as long as the process lives.
type expiringKeys struct {
	// ctx carries the HTTP client the keys are fetched with.
	ctx     context.Context
	url     string
	mu      sync.Mutex
	current *oidc.RemoteKeySet
	expires time.Time
}

func (k *expiringKeys) VerifySignature(ctx context.Context, token string) ([]byte, error) {
	k.mu.Lock()
	if now := time.Now(); k.current == nil || !now.Before(k.expires) {
		k.current, k.expires = oidc.NewRemoteKeySet(k.ctx, k.url), now.Add(keysLifetime)
	}
	keys := k.current
	k.mu.Unlock()
	return keys.VerifySignature(ctx, token)
}
