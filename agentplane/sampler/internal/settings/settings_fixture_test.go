package settings

import (
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func fixtureSettings() Settings {
	platformInstructions := "Fixture Agentplane guidance."
	return Settings{
		EnvironmentNamespace: "fixture",
		Models:               map[string]ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections:          map[string]ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: AuthConfig{Type: AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL:          "postgresql://fixture.invalid/sampler", ListenTLS: &peertls.ServerTLSListener{Listen: "127.0.0.1:8083", CertFile: "/fixture/tls.crt", KeyFile: "/fixture/tls.key"}, PlatformInstructions: &platformInstructions, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}
