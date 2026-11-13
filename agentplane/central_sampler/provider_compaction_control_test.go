package centralsampler

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
)

func TestCompactionRecoveryRejectsChangedSuffixOrEffortPolicy(t *testing.T) {
	for _, field := range []string{"keep_recent_turns", "per_message_effort"} {
		t.Run(field, func(t *testing.T) {
			store := newTestStore(t)
			session, err := openStoreSession(t, store, &samplerpb.CreateSessionRequest{
				SessionId: uuid.NewString(),
				Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model", ReasoningEffort: "high"},
			},
				"fixture-account", protocol.APIStyleAnthropicMessages)
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
			admitProviderCommand(t, store, owner, session.ID, "compaction-control", true)
			config := anthropicTestConfig()
			config.AnthropicMessages.Compaction = &anthropic.CompactionConfig{MaxTokens: 1000}
			connection := samplersettings.ConnectionConfig{
				BaseURL: "https://provider.example.invalid",
				Auth:    samplersettings.AuthConfig{Type: samplersettings.AuthTypeAnthropicAPIKey, Credential: "fixture-key"},
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			adapter, err := adapterFor(config)
			if err != nil {
				t.Fatal(err)
			}
			plan, err := adapter.PrepareCompaction(conversation, session, &ExecutionState{}, protocol.SessionAttribution{}, "")
			if err != nil {
				t.Fatal(err)
			}
			intentID, _, err := store.BeginCompaction(t.Context(), owner, providerRequestIntent(PendingIntentCompaction, connection, config),
				conversation.Context(), plan.Prepare, providerAttemptTestPolicy())
			if err != nil {
				t.Fatal(err)
			}
			if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
				Class: ProviderAttemptTransient, Reason: "upstream unavailable", RetryAfter: time.Second,
			}); err != nil {
				t.Fatal(err)
			}
			if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()-interval '1 second' WHERE id=$1`, intentID); err != nil {
				t.Fatal(err)
			}
			if field == "keep_recent_turns" {
				config.AnthropicMessages.Compaction.KeepRecentTurns = 1
			} else {
				config.AnthropicMessages.PerMessageEffort = false
			}
			runtime := &Runtime{store: store, settings: samplersettings.Settings{
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
