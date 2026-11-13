package residentruntime

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
)

func TestFinishProviderResolvesAmbiguousPersistenceWithoutResampling(t *testing.T) {
	for _, committed := range []bool{false, true} {
		t.Run(map[bool]string{false: "retry_persistence", true: "commit_reply_lost"}[committed], func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			admitProviderCommand(t, store, owner, session.ID, "ambiguous-finish", false)
			config := samplersettings.ModelConfig{
				Connection: "fixture-connection", ProviderModel: "fixture-provider-model", API: protocol.APIStyleResponses,
				ExecEncoding: protocol.ExecEncodingFreeform, Responses: responses.Config{Dialect: responses.DialectLite},
			}
			request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}, config)
			preparation := &sessionstore.ProviderRequestPreparation{
				ModelConfig: config, SessionEffort: session.Effort, SessionConfiguration: append([]byte(nil), session.Configuration...),
			}
			intentID, _, err := store.Begin(t.Context(), owner, request, preparation, providerAttemptTestPolicyPointer())
			if err != nil {
				t.Fatal(err)
			}
			message := responses.Item{
				ID: "persisted-once", Type: responses.ItemMessage, Role: "assistant",
				Content: json.RawMessage(`[{"type":"output_text","text":"persisted once"}]`),
			}
			records := nativeResponsesRecords(t, message)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			writes := 0
			runtime := &Runtime{store: store, settings: samplersettings.Settings{RPCTimeoutSeconds: 5}}
			err = runtime.finishProvider(ctx, owner, intentID, func(writeCtx context.Context) error {
				writes++
				if writes == 1 {
					if committed {
						if err := store.Finish(writeCtx, owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, records, nil); err != nil {
							t.Fatal(err)
						}
					}
					// A deadline/cancellation can race the commit reply. The complete response
					// remains in memory, so only its persistence may be retried.
					cancel()
					return context.DeadlineExceeded
				}
				return store.Finish(writeCtx, owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, records, nil)
			})
			wantWrites := 2
			if committed {
				wantWrites = 1
			}
			if err != nil || writes != wantWrites {
				t.Fatalf("finish err=%v writes=%d, want %d", err, writes, wantWrites)
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			count := 0
			for _, item := range responsesItems(t, conversation.Transcript) {
				if item.ID != message.ID {
					continue
				}
				text, err := responses.Text(item)
				if err != nil {
					t.Fatal(err)
				}
				if text == "persisted once" {
					count++
				}
			}
			if count != 1 || conversation.Pending != nil {
				t.Fatalf("result count=%d pending=%v", count, conversation.Pending)
			}
			if _, _, err := store.BeginSamplingRetry(t.Context(), owner, intentID, providerAttemptTestPolicy()); !errors.Is(err, protocol.ErrPrecondition) {
				t.Fatalf("committed result became retryable: %v", err)
			}
		})
	}
}
