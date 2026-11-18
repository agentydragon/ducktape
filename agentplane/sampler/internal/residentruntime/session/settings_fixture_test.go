package session

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func fixtureSettings() samplersettings.Settings {
	platformInstructions := "Fixture Agentplane guidance."
	return samplersettings.Settings{
		EnvironmentNamespace: "fixture",
		Models:               map[string]samplersettings.ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections:          map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL:          "postgresql://fixture.invalid/sampler", ListenTLS: &peertls.ServerTLSListener{Listen: "127.0.0.1:8083", CertFile: "/fixture/tls.crt", KeyFile: "/fixture/tls.key"}, PlatformInstructions: &platformInstructions, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}

func TestIndependentSetupTimeoutSettings(t *testing.T) {
	settings := fixtureSettings()
	if got, want := settings.SetupTimeout(), time.Hour; got != want {
		t.Fatalf("default setup timeout=%s, want %s", got, want)
	}
	configuredSetupTimeout := 17
	settings.SetupTimeoutSeconds = &configuredSetupTimeout
	if err := settings.Validate(); err != nil {
		t.Fatal(err)
	}
	if got, want := settings.SetupTimeout(), 17*time.Second; got != want {
		t.Fatalf("configured setup timeout=%s, want %s", got, want)
	}
}
