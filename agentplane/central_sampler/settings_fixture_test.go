package centralsampler

import (
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
)

func fixtureSettings() samplersettings.Settings {
	platformInstructions := "Fixture Agentplane guidance."
	return samplersettings.Settings{
		Models:      map[string]samplersettings.ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections: map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL: "postgresql://fixture.invalid/sampler", Listen: "127.0.0.1:0", Audience: "test-sampler", AllowedAccounts: []string{"test/client"}, PlatformInstructions: &platformInstructions, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, TurnTimeoutSeconds: 40, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}
