package session

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/google/uuid"
)

func TestCompactionRecoveryRejectsChangedPolicy(t *testing.T) {
	for _, field := range []string{"keep_recent_turns", "per_message_effort", "replacement_template"} {
		t.Run(field, func(t *testing.T) {
			store := runtimetesting.NewStore(t, integrationDatabase)
			api, effort := protocol.APIStyleAnthropicMessages, "high"
			if field == "replacement_template" {
				api, effort = protocol.APIStyleChatCompletions, ""
			}
			session, err := runtimetesting.OpenStoreSession(t, store, &sessionconfig.Configuration{
				SessionId: uuid.NewString(),
				Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model", ReasoningEffort: effort},
			},
				"fixture-account", api)
			if err != nil {
				t.Fatal(err)
			}
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.AppendInput(t.Context(), owner, strings.Repeat("original conversation ", 64)); err != nil {
				t.Fatal(err)
			}
			if api == protocol.APIStyleChatCompletions {
				if err := store.AppendInput(t.Context(), owner, "retained turn"); err != nil {
					t.Fatal(err)
				}
			}
			admitProviderCommand(t, store, owner, session.ID, "compaction-control", true)
			config := testsupport.AnthropicTestConfig()
			config.AnthropicMessages.Compaction = &anthropic.CompactionConfig{MaxTokens: 1000}
			connection := samplersettings.ConnectionConfig{
				BaseURL: "https://provider.example.invalid",
				Auth:    samplersettings.AuthConfig{Type: samplersettings.AuthTypeAnthropicAPIKey, Credential: "fixture-key"},
			}
			if api == protocol.APIStyleChatCompletions {
				config = samplersettings.ModelConfig{
					API: api, ProviderModel: "fixture-model", Connection: "chat", ExecEncoding: protocol.ExecEncodingJSON,
					ChatCompaction: &chatcompletions.CompactionConfig{
						MaxTokens: 1000, KeepRecentTurns: 1,
						Replacement: &chatcompletions.CompactionReplacement{Role: "user", Template: "Handoff: {{.Summary}}"},
					},
				}
				connection.Auth.Type = samplersettings.AuthTypeBearer
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			adapter, err := adapterFor(config)
			if err != nil {
				t.Fatal(err)
			}
			plan, err := adapter.PrepareCompaction(conversation, session, protocol.SessionAttribution{}, "")
			if err != nil {
				t.Fatal(err)
			}
			intentID, _, err := store.BeginCompaction(t.Context(), owner, sessionstore.ProviderRequestIntent(sessionstore.PendingIntentCompaction, connection, config),
				conversation.Context(), plan.Preparation, providerAttemptTestPolicy())
			if err != nil {
				t.Fatal(err)
			}
			if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, sessionstore.ProviderAttemptFailure{
				Class: sessionstore.ProviderAttemptTransient, Reason: "upstream unavailable", RetryAfter: time.Second,
			}); err != nil {
				t.Fatal(err)
			}
			if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()-interval '1 second' WHERE id=$1`, intentID); err != nil {
				t.Fatal(err)
			}
			switch field {
			case "keep_recent_turns":
				config.AnthropicMessages.Compaction.KeepRecentTurns = 1
			case "per_message_effort":
				config.AnthropicMessages.PerMessageEffort = false
			case "replacement_template":
				config.ChatCompaction.Replacement.Template = "<checkpoint>{{.Summary}}</checkpoint>"
			}
			runtime := &Driver{store: store, settings: samplersettings.Settings{
				Models:      map[string]samplersettings.ModelConfig{session.Model: config},
				Connections: map[string]samplersettings.ConnectionConfig{config.Connection: connection},
			}}
			if _, err := runtime.providerFrontier(t.Context(), owner, session); !errors.Is(err, errProviderCommandFailed) {
				t.Fatalf("changed compaction policy was allowed to consume a frozen request: %v", err)
			}
			command, err := store.GetCommand(t.Context(), session.ID, "compaction-control")
			if err != nil || command.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED ||
				!strings.Contains(command.GetFailureReason(), "changed during recovery") {
				t.Fatalf("command=%v err=%v", command, err)
			}
			got, err := store.GetSession(t.Context(), session.ID, "")
			if err != nil || got.Blocked || got.ActiveCommand != nil {
				t.Fatalf("changed policy stranded the Session: %+v err=%v", got, err)
			}
		})
	}
}
