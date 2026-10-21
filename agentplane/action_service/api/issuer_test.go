package api

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	operatorAudience = "test-operator-audience"
	operatorSubject  = "test-operator"
	operatorKeyID    = "test-key"
)

// issuer is a local OIDC issuer: it publishes its signing key at /jwks/ and signs operator tokens.
type issuer struct {
	url string
	key *rsa.PrivateKey
}

func newIssuer(t *testing.T) *issuer {
	t.Helper()
	i := &issuer{key: newKey(t)}
	keys := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
		{Key: &i.key.PublicKey, KeyID: operatorKeyID, Algorithm: string(jose.RS256), Use: "sig"},
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
	i.url = server.URL + "/"
	return i
}

func newKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func (i *issuer) settings(profile TokenProfile) OperatorOIDCSettings {
	return OperatorOIDCSettings{Issuer: i.url, Audience: operatorAudience, JWKSURI: i.url + "jwks/", TokenProfile: profile}
}

// claims are an operator token's claims as the issuer signs them by default, with changes applied: a nil value
// leaves its claim out.
func (i *issuer) claims(changes map[string]any) map[string]any {
	now := time.Now()
	claims := map[string]any{
		"iss": i.url, "aud": operatorAudience, "azp": operatorAudience, "sub": operatorSubject,
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

// sign signs claims under the key ID with key.
func sign(t *testing.T, key jose.SigningKey, keyID string, claims map[string]any) string {
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

// token is a token the issuer signed with the published key.
func (i *issuer) token(t *testing.T, changes map[string]any) string {
	t.Helper()
	return sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: i.key}, operatorKeyID, i.claims(changes))
}

func (i *issuer) operator() store.OperatorPrincipal {
	return store.OperatorPrincipal{Issuer: i.url, Subject: operatorSubject}
}
