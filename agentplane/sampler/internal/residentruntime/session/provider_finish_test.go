package session

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func TestFinishProviderResolvesAmbiguousPersistenceWithoutResampling(t *testing.T) {
	for _, committed := range []bool{false, true} {
		t.Run(map[bool]string{false: "retry_persistence", true: "commit_reply_lost"}[committed], func(t *testing.T) {
			store, session, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
			admitProviderCommand(t, store, owner, session.ID, "ambiguous-finish", false)
			config := samplersettings.ModelConfig{
				Connection: "fixture-connection", ProviderModel: "fixture-provider-model", API: protocol.APIStyleResponses,
				ExecEncoding: protocol.ExecEncodingFreeform, Responses: responses.Config{Dialect: responses.DialectLite},
			}
			request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}, config)
			preparation := &providerrequest.Preparation{
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
			records := testsupport.NativeResponsesRecords(t, message)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			writes := 0
			runtime := &Driver{store: store, settings: samplersettings.Settings{RPCTimeoutSeconds: 5}}
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
			for _, item := range testsupport.ResponsesItems(t, conversation.Transcript) {
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

			// The turn deadline can win controlledAdvance's select after this joined
			// response was committed. Its terminal transcript remains authoritative.
			if settled, err := runtime.settleExpiredProviderCommand(t.Context(), owner, session.ID, "another-command"); err != nil || settled {
				t.Fatalf("completed transcript settled another command: settled=%v err=%v", settled, err)
			}
			staleOwner := owner
			staleOwner.Generation++
			if settled, err := runtime.settleExpiredProviderCommand(t.Context(), staleOwner, session.ID, "ambiguous-finish"); err == nil || settled {
				t.Fatalf("stale owner settled completed turn: settled=%v err=%v", settled, err)
			}
			settled, err := runtime.settleExpiredProviderCommand(t.Context(), owner, session.ID, "ambiguous-finish")
			if err != nil || !settled {
				t.Fatalf("expired completed provider turn settled=%v, err=%v", settled, err)
			}
			command, err := store.GetCommand(t.Context(), session.ID, "ambiguous-finish")
			if err != nil || command.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED {
				t.Fatalf("completed provider command=%v, err=%v", command, err)
			}
			state, err := store.GetSession(t.Context(), session.ID, "")
			if err != nil || state.Blocked || state.ActiveTurn != nil || state.ActiveCommand != nil {
				t.Fatalf("completed provider turn left Session unavailable: %+v, %v", state, err)
			}
			admitProviderCommand(t, store, owner, session.ID, "next-input", false)
			if settled, err := runtime.settleExpiredProviderCommand(t.Context(), owner, session.ID, "next-input"); err != nil || settled {
				t.Fatalf("unfinished next input inherited prior completion: settled=%v err=%v", settled, err)
			}
			toolIntent, _, err := store.Begin(t.Context(), owner, request, preparation, providerAttemptTestPolicyPointer())
			if err != nil {
				t.Fatal(err)
			}
			tool := responses.Item{Type: responses.ItemFunctionCall, ID: "native-call-item", CallID: "native-call", Name: "exec", Arguments: `{"cmd":"must not be dispatched"}`}
			if err := store.Finish(t.Context(), owner, toolIntent, map[string]any{"response": map[string]any{"status": "completed"}}, testsupport.NativeResponsesRecords(t, tool), nil); err != nil {
				t.Fatal(err)
			}
			if settled, err := runtime.settleExpiredProviderCommand(t.Context(), owner, session.ID, "next-input"); err != nil || settled {
				t.Fatalf("pending tool call treated as complete: settled=%v err=%v", settled, err)
			}
			conversation, err = store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			calls, err := conversation.Transcript.PendingCalls()
			if err != nil || len(calls) != 1 {
				t.Fatalf("pending calls=%v err=%v", calls, err)
			}
			if _, _, err := store.Begin(t.Context(), owner, sessionstore.IntentPayload{Kind: sessionstore.PendingIntentCell, Call: &calls[0]}, nil, nil); err != nil {
				t.Fatal(err)
			}
			if settled, err := runtime.settleExpiredProviderCommand(t.Context(), owner, session.ID, "next-input"); err != nil || settled {
				t.Fatalf("unresolved native intent treated as complete: settled=%v err=%v", settled, err)
			}
		})
	}
}
