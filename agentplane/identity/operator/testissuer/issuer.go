// Package testissuer provides a local OIDC issuer for operator token tests.
package testissuer

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	Audience = "test-operator-audience"
	Subject  = "test-operator"
	KeyID    = "test-key"
)

// Issuer publishes its signing key at /jwks/ and signs operator tokens.
type Issuer struct {
	URL string
	Key *rsa.PrivateKey
}

func New(t testing.TB) *Issuer {
	t.Helper()
	i := &Issuer{Key: NewKey(t)}
	keys := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
		{Key: &i.Key.PublicKey, KeyID: KeyID, Algorithm: string(jose.RS256), Use: "sig"},
	}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/jwks/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(keys); err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(server.Close)
	i.URL = server.URL + "/"
	return i
}

func NewKey(t testing.TB) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func (i *Issuer) Settings(profile operator.TokenProfile) operator.Settings {
	return operator.Settings{Issuer: i.URL, Audience: Audience, JWKSURI: i.URL + "jwks/", TokenProfile: profile}
}

// Claims applies changes to the default operator claims; a nil value omits the claim.
func (i *Issuer) Claims(changes map[string]any) map[string]any {
	now := time.Now()
	claims := map[string]any{
		"iss": i.URL, "aud": Audience, "azp": Audience, "sub": Subject,
		"iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	}
	for name, value := range changes {
		if value == nil {
			delete(claims, name)
		} else {
			claims[name] = value
		}
	}
	return claims
}

func Sign(t testing.TB, key jose.SigningKey, keyID string, claims map[string]any) string {
	t.Helper()
	signer, err := jose.NewSigner(key, (&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", keyID))
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.Signed(signer).Claims(claims).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func (i *Issuer) Token(t testing.TB, changes map[string]any) string {
	t.Helper()
	return Sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: i.Key}, KeyID, i.Claims(changes))
}
