// Package testauth provides signed operator ID tokens for Agentplane server tests.
package testauth

import (
	"crypto/ed25519"
	"crypto/rand"
	"database/sql"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	jose "github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const issuer = "https://operator-tokens.test"

// Fixture pairs an app auth boundary with a bearer ID token accepted by that boundary.
type Fixture struct {
	Auth  *auth.Auth
	token string
}

// New creates an auth boundary pinned to a generated signing key and its matching operator ID token.
func New(t testing.TB) *Fixture {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	authentication, err := auth.New(auth.Config{
		OIDC: &auth.OIDCConfig{
			Issuer: issuer, ClientID: "test-client", ClientSecret: "test-client-secret",
			SessionSecret: "test-session-secret", PublicBaseURL: "https://agentplane.test",
			StaticKeys: []ed25519.PublicKey{public},
		},
		Database: new(sql.DB),
	})
	if err != nil {
		t.Fatal(err)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.EdDSA, Key: private}, nil)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	token, err := jwt.Signed(signer).Claims(map[string]any{
		"iss": issuer, "aud": "test-client", "sub": "test-operator-subject",
		"preferred_username": "test-operator", "iat": now.Unix(), "exp": now.Add(time.Minute).Unix(),
	}).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	return &Fixture{Auth: authentication, token: token}
}

// Authorize sets the fixture's operator ID token as the request's bearer credential.
func (f *Fixture) Authorize(request *http.Request) {
	request.Header.Set("Authorization", "Bearer "+f.token)
}
