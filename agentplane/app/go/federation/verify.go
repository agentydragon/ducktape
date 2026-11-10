package federation

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	clockSkew        = 30 * time.Second
	keyCacheLifetime = 5 * time.Minute
)

type principal struct {
	Issuer  string
	Subject string
}

type tokenVerifier struct {
	settings ProviderConfig
	client   *http.Client
	now      func() time.Time

	mu      sync.Mutex
	keys    map[string]*jose.JSONWebKey
	invalid map[string]struct{}
	expires time.Time
}

func newTokenVerifier(settings ProviderConfig, client *http.Client) (*tokenVerifier, error) {
	if err := validateProvider(settings); err != nil {
		return nil, err
	}
	return &tokenVerifier{settings: settings, client: client, now: time.Now}, nil
}

func (v *tokenVerifier) verify(ctx context.Context, token string, now time.Time) (principal, error) {
	parsed, err := jwt.ParseSigned(token, []jose.SignatureAlgorithm{jose.RS256})
	if err != nil || len(parsed.Headers) != 1 {
		return principal{}, tokenInvalid()
	}
	header := parsed.Headers[0]
	if header.Algorithm != string(jose.RS256) || strings.TrimSpace(header.KeyID) == "" || hasCriticalHeader(token) {
		return principal{}, tokenInvalid()
	}
	key, err := v.signingKey(ctx, header.KeyID)
	if err != nil {
		return principal{}, err
	}
	var raw map[string]json.RawMessage
	if err := parsed.Claims(key.Key, &raw); err != nil {
		return principal{}, tokenInvalid()
	}
	issuer, issuerOK := rawString(raw, "iss")
	subject, subjectOK := rawString(raw, "sub")
	audience, audienceOK := rawString(raw, "aud")
	issuedAt, issuedAtOK := rawInt(raw, "iat")
	expiresAt, expiresAtOK := rawInt(raw, "exp")
	if !issuerOK || issuer != v.settings.Issuer || !subjectOK || strings.TrimSpace(subject) == "" ||
		!audienceOK || audience != v.settings.Audience || !issuedAtOK || !expiresAtOK || expiresAt <= issuedAt ||
		!time.Unix(expiresAt, 0).After(now.Add(-clockSkew)) || time.Unix(issuedAt, 0).After(now.Add(clockSkew)) {
		return principal{}, tokenInvalid()
	}
	if rawNBF, present := raw["nbf"]; present {
		nbf, ok := rawIntValue(rawNBF)
		if !ok || time.Unix(nbf, 0).After(now.Add(clockSkew)) {
			return principal{}, tokenInvalid()
		}
	}
	if _, present := raw["azp"]; present {
		authorizedParty, ok := rawString(raw, "azp")
		if !ok || authorizedParty != v.settings.Audience {
			return principal{}, tokenInvalid()
		}
	} else if v.settings.TokenProfile == profileAuthentik {
		return principal{}, tokenInvalid()
	}
	return principal{Issuer: issuer, Subject: subject}, nil
}

func (v *tokenVerifier) signingKey(ctx context.Context, kid string) (*jose.JSONWebKey, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if !v.expires.After(v.now()) {
		if err := v.refreshKeys(ctx); err != nil {
			return nil, err
		}
	}
	if _, unusable := v.invalid[kid]; unusable {
		return nil, verificationUnavailable()
	}
	if key := v.keys[kid]; key != nil {
		return key, nil
	}
	// An unknown kid may be a rotated key, so force one immediate refresh even if the
	// previous key set has not reached its normal five-minute lifetime.
	if err := v.refreshKeys(ctx); err != nil {
		return nil, err
	}
	if _, unusable := v.invalid[kid]; unusable {
		return nil, verificationUnavailable()
	}
	if key := v.keys[kid]; key != nil {
		return key, nil
	}
	return nil, tokenInvalid()
}

func (v *tokenVerifier) refreshKeys(ctx context.Context) error {
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, v.settings.JWKSURI, nil)
	if err != nil {
		return verificationUnavailable()
	}
	response, err := v.client.Do(req)
	if err != nil {
		return upstreamError(req, 0, errorType(err))
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return upstreamError(req, response.StatusCode, "HTTPStatusError")
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return upstreamError(req, 0, errorType(err))
	}
	var keySet jose.JSONWebKeySet
	if err := json.Unmarshal(body, &keySet); err != nil || len(keySet.Keys) == 0 {
		return verificationUnavailable()
	}
	keys := make(map[string]*jose.JSONWebKey, len(keySet.Keys))
	invalid := make(map[string]struct{})
	for i := range keySet.Keys {
		key := &keySet.Keys[i]
		if strings.TrimSpace(key.KeyID) == "" || key.Use != "" && key.Use != "sig" {
			continue
		}
		if key.Algorithm != "" && key.Algorithm != string(jose.RS256) {
			invalid[key.KeyID] = struct{}{}
			continue
		}
		if _, ok := key.Key.(*rsa.PublicKey); !ok {
			invalid[key.KeyID] = struct{}{}
			continue
		}
		if keys[key.KeyID] == nil {
			keys[key.KeyID] = key
		}
	}
	if len(keys) == 0 {
		return verificationUnavailable()
	}
	v.keys, v.invalid = keys, invalid
	v.expires = v.now().Add(keyCacheLifetime)
	return nil
}

func hasCriticalHeader(token string) bool {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return true
	}
	header, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return true
	}
	var values map[string]json.RawMessage
	if json.Unmarshal(header, &values) != nil {
		return true
	}
	_, present := values["crit"]
	return present
}

func rawString(raw map[string]json.RawMessage, key string) (string, bool) {
	value, ok := raw[key]
	if !ok {
		return "", false
	}
	var text string
	if json.Unmarshal(value, &text) != nil {
		return "", false
	}
	return text, true
}

func rawInt(raw map[string]json.RawMessage, key string) (int64, bool) {
	value, ok := raw[key]
	if !ok {
		return 0, false
	}
	return rawIntValue(value)
}

func rawIntValue(value json.RawMessage) (int64, bool) {
	text := strings.TrimSpace(string(value))
	if text == "" || strings.ContainsAny(text, ".eE") {
		return 0, false
	}
	parsed, err := strconv.ParseInt(text, 10, 64)
	return parsed, err == nil
}

func tokenInvalid() error {
	return federationError("operator_federation_token_invalid", http.StatusForbidden)
}

func verificationUnavailable() error {
	return federationError("operator_federation_verification_unavailable", http.StatusServiceUnavailable)
}
