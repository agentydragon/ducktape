package operator_test

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/go-jose/go-jose/v4"
)

func TestAnOperatorTokenIsAcceptedOnlyAsItsIssuerSignedItForTheService(t *testing.T) {
	i := newIssuer(t)
	now := time.Now()
	for _, tc := range []struct {
		name     string
		profile  operator.TokenProfile
		token    string
		accepted bool
	}{
		{"as signed", operator.ProfileAuthentik, i.token(t, nil), true},
		{"expired within the clock skew", operator.ProfileAuthentik, i.token(t, map[string]any{"exp": now.Add(-10 * time.Second).Unix()}), true},
		{"expired beyond the clock skew", operator.ProfileAuthentik, i.token(t, map[string]any{"exp": now.Add(-time.Minute).Unix()}), false},
		{"for another audience", operator.ProfileAuthentik, i.token(t, map[string]any{"aud": "test-other-audience", "azp": "test-other-audience"}), false},
		{"for two audiences", operator.ProfileAuthentik, i.token(t, map[string]any{"aud": []string{operatorAudience, "test-other-audience"}}), false},
		{"authorized for another party", operator.ProfileAuthentik, i.token(t, map[string]any{"azp": "test-other-party"}), false},
		{"without an authorized party", operator.ProfileAuthentik, i.token(t, map[string]any{"azp": nil}), false},
		{"without an authorized party from Dex", operator.ProfileDex, i.token(t, map[string]any{"azp": nil}), true},
		{"authorized for another party from Dex", operator.ProfileDex, i.token(t, map[string]any{"azp": "test-other-party"}), false},
		{"without a subject", operator.ProfileAuthentik, i.token(t, map[string]any{"sub": nil}), false},
		{"with a blank subject", operator.ProfileAuthentik, i.token(t, map[string]any{"sub": " "}), false},
		{"from another issuer", operator.ProfileAuthentik, i.token(t, map[string]any{"iss": "https://test-other-issuer.example/"}), false},
		{
			"signed by a key the issuer does not publish", operator.ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: newKey(t)}, operatorKeyID, i.claims(nil)), false,
		},
		{
			"under a key ID the issuer does not publish", operator.ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.RS256, Key: i.key}, "test-unknown-key", i.claims(nil)), false,
		},
		{
			"signed with a shared secret", operator.ProfileAuthentik,
			sign(t, jose.SigningKey{Algorithm: jose.HS256, Key: []byte("test-shared-secret-of-32-bytes!!")}, operatorKeyID, i.claims(nil)), false,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			verifier, err := operator.NewVerifier(i.settings(tc.profile))
			if err != nil {
				t.Fatal(err)
			}
			got, err := verifier.Verify(t.Context(), tc.token)
			switch {
			case tc.accepted && (err != nil || got != (operator.Subject{Issuer: i.url, Subject: operatorSubject})):
				t.Errorf("refused: %v, %+v", err, got)
			case !tc.accepted && err == nil:
				t.Errorf("accepted as %+v", got)
			}
		})
	}
}

func TestMatchesUnverifiedIssuerOnlySelectsTheVerifier(t *testing.T) {
	i := newIssuer(t)
	verifier, err := operator.NewVerifier(i.settings(operator.ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	if !verifier.MatchesUnverifiedIssuer(i.token(t, nil)) {
		t.Fatal("issuer token did not select its verifier")
	}
	if verifier.MatchesUnverifiedIssuer("not-a-jwt") {
		t.Fatal("malformed token selected the verifier")
	}
}
