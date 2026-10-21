package api

import (
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
)

func TestAnOperatorTokenIsAcceptedOnlyAsItsIssuerSignedItForTheService(t *testing.T) {
	i := newIssuer(t)
	now := time.Now()
	for _, tc := range []struct {
		name     string
		profile  TokenProfile
		token    string
		accepted bool
	}{
		{"as signed", ProfileAuthentik, i.token(t, nil), true},
		{"expired within the clock skew", ProfileAuthentik, i.token(t, map[string]any{"exp": now.Add(-10 * time.Second).Unix()}), true},
		{"expired beyond the clock skew", ProfileAuthentik, i.token(t, map[string]any{"exp": now.Add(-time.Minute).Unix()}), false},
		{"for another audience", ProfileAuthentik, i.token(t, map[string]any{"aud": "test-other-audience", "azp": "test-other-audience"}), false},
		{"for two audiences", ProfileAuthentik, i.token(t, map[string]any{"aud": []string{operatorAudience, "test-other-audience"}}), false},
		{"authorized for another party", ProfileAuthentik, i.token(t, map[string]any{"azp": "test-other-party"}), false},
		{"without an authorized party", ProfileAuthentik, i.token(t, map[string]any{"azp": nil}), false},
		{"without an authorized party from Dex", ProfileDex, i.token(t, map[string]any{"azp": nil}), true},
		{"authorized for another party from Dex", ProfileDex, i.token(t, map[string]any{"azp": "test-other-party"}), false},
		{"without a subject", ProfileAuthentik, i.token(t, map[string]any{"sub": nil}), false},
		{"with a blank subject", ProfileAuthentik, i.token(t, map[string]any{"sub": " "}), false},
		{"from another issuer", ProfileAuthentik, i.token(t, map[string]any{"iss": "https://test-other-issuer.example/"}), false},
		{
			"signed by a key the issuer does not publish", ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: newKey(t)}, operatorKeyID, i.claims(nil)), false,
		},
		{
			"under a key ID the issuer does not publish", ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: i.key}, "test-unknown-key", i.claims(nil)), false,
		},
		{
			"signed with a shared secret", ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.HS256, Key: []byte("test-shared-secret-of-32-bytes!!")}, operatorKeyID, i.claims(nil)), false,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			verifier, err := NewOperatorVerifier(i.settings(tc.profile))
			if err != nil {
				t.Fatal(err)
			}
			operator, err := verifier.Verify(t.Context(), tc.token)
			switch {
			case tc.accepted && (err != nil || operator != i.operator()):
				t.Errorf("refused: %v, %+v", err, operator)
			case !tc.accepted && err == nil:
				t.Errorf("accepted as %+v", operator)
			}
		})
	}
}
