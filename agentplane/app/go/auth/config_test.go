package auth

import (
	"os"
	"testing"
)

func clearOIDCSecretEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{
		"AGENTPLANE_OIDC_CLIENT_ID",
		"AGENTPLANE_OIDC_CLIENT_SECRET",
		"AGENTPLANE_OIDC_SESSION_SECRET",
	} {
		t.Setenv(name, "")
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
	}
}

func setOIDCSecretEnv(t *testing.T) {
	t.Helper()
	t.Setenv("AGENTPLANE_OIDC_CLIENT_ID", "client")
	t.Setenv("AGENTPLANE_OIDC_CLIENT_SECRET", "client-secret")
	t.Setenv("AGENTPLANE_OIDC_SESSION_SECRET", "session-secret")
}

func TestLoadOIDCRequiresTheStaticSectionForSecretInputs(t *testing.T) {
	clearOIDCSecretEnv(t)
	if config, err := LoadOIDC(nil); err != nil || config != nil {
		t.Fatalf("unconfigured OIDC = (%+v, %v), want (nil, nil)", config, err)
	}
	t.Setenv("AGENTPLANE_OIDC_CLIENT_ID", "client")
	if config, err := LoadOIDC(nil); err == nil || config != nil {
		t.Fatalf("partial Secret overlay = (%+v, %v), want an error", config, err)
	}
}

func TestLoadOIDCRequiresCompleteSecretInputsWhenStaticSettingsExist(t *testing.T) {
	clearOIDCSecretEnv(t)
	settings := &OIDCSettings{Issuer: "https://auth.example.test/", PublicBaseURL: "https://app.example.test"}
	if config, err := LoadOIDC(settings); err == nil || config != nil {
		t.Fatalf("OIDC without Secret inputs = (%+v, %v), want an error", config, err)
	}
}

func TestLoadOIDCUsesDefaultsAndPreservesExplicitTokenRenewZero(t *testing.T) {
	clearOIDCSecretEnv(t)
	setOIDCSecretEnv(t)
	settings := &OIDCSettings{Issuer: "https://auth.example.test/", PublicBaseURL: "https://app.example.test"}
	config, err := LoadOIDC(settings)
	if err != nil {
		t.Fatal(err)
	}
	if config.SessionMaxSeconds != 604800 || config.SessionIdleSeconds != 86400 ||
		config.SessionActivityStepSeconds != 300 || config.TokenRenewBeforeSeconds != 30 {
		t.Fatalf("OIDC defaults = %+v", config)
	}

	zero := 0
	settings.TokenRenewBeforeSeconds = &zero
	config, err = LoadOIDC(settings)
	if err != nil {
		t.Fatal(err)
	}
	if config.TokenRenewBeforeSeconds != 0 {
		t.Fatalf("explicit token renew zero changed to %d", config.TokenRenewBeforeSeconds)
	}
}

func TestLoadOIDCRejectsInvalidStaticSettings(t *testing.T) {
	clearOIDCSecretEnv(t)
	setOIDCSecretEnv(t)
	for _, settings := range []*OIDCSettings{
		{Issuer: "http://auth.example.test/", PublicBaseURL: "https://app.example.test"},
		{Issuer: "https://auth.example.test/", PublicBaseURL: "https://app.example.test?tenant=x"},
		{Issuer: "https://auth.example.test/", PublicBaseURL: "https://app.example.test", SessionIdleSeconds: intPointer(0)},
		{Issuer: "https://auth.example.test/", PublicBaseURL: "https://app.example.test", TokenRenewBeforeSeconds: intPointer(-1)},
	} {
		if config, err := LoadOIDC(settings); err == nil || config != nil {
			t.Errorf("accepted invalid OIDC settings %+v: config=%+v err=%v", settings, config, err)
		}
	}
}

func intPointer(value int) *int { return &value }
