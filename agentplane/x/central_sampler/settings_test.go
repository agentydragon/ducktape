package centralsampler

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fixtureSettings() Settings {
	return Settings{
		DatabaseURL: "postgresql://fixture.invalid/sampler", Listen: "127.0.0.1:0", ServiceName: "fixture", Providers: map[string]Provider{"responses": {Endpoint: "https://model.invalid/responses", Credential: "test-credential", Models: []string{"test-model"}, ReasoningEfforts: []string{"", "high"}}}, Audience: "test-sampler", AllowedAccounts: []string{"test/client"}, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorURL: "ws://executor.invalid", MaxLiveSessions: 2, HostRSSBytes: 1024 * 1024, TurnTimeoutSeconds: 40, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}

func TestSettingsRejectPartialAndUnknownConfiguration(t *testing.T) {
	for name, edit := range map[string]func(*Settings){
		"history token missing":   func(s *Settings) { s.HistoryTarget = "history.invalid:8080" },
		"gateway token missing":   func(s *Settings) { s.DirectExecutorURL = ""; s.GatewayTarget = "gateway.invalid:8080" },
		"two executor transports": func(s *Settings) { s.GatewayTarget = "gateway.invalid:8080"; s.GatewayTokenFile = "/fixture/token" },
		"no executor transport":   func(s *Settings) { s.DirectExecutorURL = "" },
		"unknown template": func(s *Settings) {
			s.DirectExecutorURL = ""
			s.GatewayTarget = "{host}:8080"
			s.GatewayTokenFile = "/fixture/token"
		},
		"zero capacity":   func(s *Settings) { s.MaxLiveSessions = 0 },
		"invalid account": func(s *Settings) { s.AllowedAccounts = []string{"test/client/other"} },
		"provider credential missing": func(s *Settings) {
			provider := s.Providers["responses"]
			provider.Credential = ""
			s.Providers["responses"] = provider
		},
		"provider credentials duplicated": func(s *Settings) {
			provider := s.Providers["responses"]
			provider.CredentialFile = "/fixture/token"
			s.Providers["responses"] = provider
		},
	} {
		t.Run(name, func(t *testing.T) {
			s := fixtureSettings()
			edit(&s)
			if s.Validate() == nil {
				t.Fatal("accepted invalid settings")
			}
		})
	}
	s := fixtureSettings()
	encoded, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	invalid := strings.TrimSuffix(string(encoded), "}") + `,"unknown":true}`
	if _, err := DecodeSettings(strings.NewReader(invalid)); err == nil {
		t.Fatal("accepted unknown setting")
	}
	invalid = strings.Replace(string(encoded), `"test-credential"`, `{"secret":"test-credential"}`, 1)
	if _, err := DecodeSettings(strings.NewReader(invalid)); err == nil {
		t.Fatal("accepted invalid credential type")
	}
	if _, err := DecodeSettings(strings.NewReader(string(encoded) + ` {}`)); err == nil {
		t.Fatal("accepted a second configuration object")
	}
}

func TestLoadSettingsUsesDatabaseURLEnvironmentOverride(t *testing.T) {
	settings := fixtureSettings()
	encoded, err := json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]any
	if err := json.Unmarshal(encoded, &values); err != nil {
		t.Fatal(err)
	}
	delete(values, "database_url")
	encoded, err = json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "settings.json")
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	want := "postgresql://rotated-user:rotated-password@database.invalid/sampler"
	t.Setenv("AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL", want)
	got, err := LoadSettings(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.DatabaseURL != want {
		t.Fatalf("database URL=%q, want environment override %q", got.DatabaseURL, want)
	}
}

func TestProviderReadsRotatingCredentialFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("first-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	provider := Provider{CredentialFile: path}
	first, err := provider.ReadCredential()
	if err != nil {
		t.Fatal(err)
	}
	if first != "first-token" {
		t.Fatalf("first credential=%q", first)
	}
	if err := os.WriteFile(path, []byte("second-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	second, err := provider.ReadCredential()
	if err != nil {
		t.Fatal(err)
	}
	if second != "second-token" {
		t.Fatalf("second credential=%q after projected file rotation", second)
	}
}

func TestGatewayTemplateCannotInjectAddress(t *testing.T) {
	s := fixtureSettings()
	s.DirectExecutorURL = ""
	s.GatewayTarget = "{sandbox}-control.{namespace}.svc.cluster.local:8080"
	s.GatewayTokenFile = "/fixture/token"
	address, err := s.GatewayAddress("test", "executor")
	if err != nil || address != "executor-control.test.svc.cluster.local:8080" {
		t.Fatalf("address=%q err=%v", address, err)
	}
	for _, name := range []string{"bad.example", "UPPER", "", strings.Repeat("a", 64), "../other", "elsewhere:1234/"} {
		if _, err := s.GatewayAddress("test", name); err == nil {
			t.Fatalf("accepted executor name %q", name)
		}
	}
}
