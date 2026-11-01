package centralsampler

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestSamplingRecoveryReconcilesOnlyTheAbandonedResponse(t *testing.T) {
	for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
		for _, cut := range []string{"no_items", "during_text", "before_terminal"} {
			t.Run(string(api)+"/"+cut, func(t *testing.T) {
				store := newTestStore(t)
				session, err := store.OpenSession(t.Context(), &samplerpb.OpenSessionRequest{
					SessionId: uuid.NewString(),
					Spec:      &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture"},
				}, "fixture-account", api)
				if err != nil {
					t.Fatal(err)
				}
				owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
				if err != nil {
					t.Fatal(err)
				}
				const turnID = "fixture-turn"
				if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
					CommandId: turnID,
					Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "Start"}},
				}, "fixture-account", nil); err != nil {
					t.Fatal(err)
				}
				if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
					t.Fatal(err)
				}
				priorIntent, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
				if err != nil {
					t.Fatal(err)
				}
				priorEvidence := &ResponseEvidence{Store: store, Owner: owner, IntentID: priorIntent}
				prior, err := sampleRecoveryFixture(t, api, "prior", "complete", priorEvidence)
				if err != nil {
					t.Fatal(err)
				}
				if err := store.Finish(t.Context(), owner, priorIntent, prior.Audit, prior.Records, prior.ContextTokens); err != nil {
					t.Fatal(err)
				}
				conversation, err := store.Read(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				calls, err := conversation.Archive.PendingCalls()
				if err != nil || len(calls) != 1 {
					t.Fatalf("prior calls=%v err=%v", calls, err)
				}
				call := calls[0]
				cellIntent, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentCell, Call: &call})
				if err != nil {
					t.Fatal(err)
				}
				output := []protocol.ContentPart{{Type: "input_text", Text: "Earlier command completed"}}
				if err := store.FinishTool(t.Context(), owner, cellIntent,
					map[string]any{"call_id": call.CallID, "output": output, "success": true}, call, output); err != nil {
					t.Fatal(err)
				}
				conversation, err = store.Read(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				beforeRecords, err := conversation.Archive.Records()
				if err != nil {
					t.Fatal(err)
				}
				intentID, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
				if err != nil {
					t.Fatal(err)
				}
				evidence := &ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}
				if _, err := sampleRecoveryFixture(t, api, "pending", cut, evidence); err == nil {
					t.Fatal("truncated stream completed")
				}
				if err := priorEvidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("unrelated")},
					&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
						ItemId: "unrelated", Kind: eventpb.ItemKind_ITEM_KIND_REASONING,
					}}}); err != nil {
					t.Fatal(err)
				}
				if api == protocol.APIStyleResponses && cut == "before_terminal" {
					// More than one reverse page must be read before reaching the item evidence.
					for range 130 {
						if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("tail")}); err != nil {
							t.Fatal(err)
						}
					}
				}
				active := turnID
				if err := store.FailExecution(t.Context(), owner, session.ID, &active, errors.New("provider stream interrupted")); err != nil {
					t.Fatal(err)
				}
				blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
				if err != nil {
					t.Fatal(err)
				}
				beforeEvents, err := store.Events(t.Context(), session.ID, 0, int(blocked.Cursor))
				if err != nil {
					t.Fatal(err)
				}
				if err := store.Release(t.Context(), owner); err != nil {
					t.Fatal(err)
				}
				recovered, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "reviewed stream", "")
				if err != nil {
					t.Fatal(err)
				}
				entries, err := store.Events(t.Context(), session.ID, 0, int(recovered.Cursor))
				if err != nil {
					t.Fatal(err)
				}
				for index, entry := range beforeEvents {
					if !proto.Equal(entry, entries[index]) {
						t.Fatalf("recovery changed retained event %d", entry.GetCursor())
					}
				}
				wantAbsent := map[string]bool{}
				if cut != "no_items" {
					if api == protocol.APIStyleResponses {
						wantAbsent["pending-reasoning"], wantAbsent["pending-text"] = true, true
					} else {
						wantAbsent["pending#0"], wantAbsent["pending#1"] = true, true
					}
					if cut == "before_terminal" {
						wantAbsent["pending-call"] = true
					}
				}
				reports := 0
				for _, entry := range entries {
					if report := entry.GetEvent().GetConversationReconciled(); report != nil {
						reports++
						if report.GetTurnId() != turnID || len(report.GetItems()) != len(wantAbsent) {
							t.Fatalf("unexpected reconciliation: %v", report)
						}
						for _, item := range report.GetItems() {
							if !wantAbsent[item.GetItemId()] || item.GetDisposition() != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT {
								t.Fatalf("unexpected item recovery: %v", item)
							}
						}
					}
				}
				if reports != 1 {
					t.Fatalf("reconciliation count=%d", reports)
				}
				batch := threadfold.EventBatch{SourceID: session.SourceID, Entries: entries}
				state, err := threadfold.Initial(session.SourceID, "fixture-projection")
				if err != nil {
					t.Fatal(err)
				}
				keys := threadfold.Touched(batch)
				priorRows := threadfold.PriorEntities{Items: map[string]*threadfold.Item{}, Commands: map[string]*threadfold.CommandSummary{}}
				for _, id := range keys.ItemIDs {
					priorRows.Items[id] = nil
				}
				for _, id := range keys.CommandIDs {
					priorRows.Commands[id] = nil
				}
				folded, err := threadfold.Advance(state, batch, priorRows)
				if err != nil {
					t.Fatal(err)
				}
				for _, item := range folded.ItemUpserts {
					if wantAbsent[item.ItemID] {
						if item.Recovery == nil || *item.Recovery != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT {
							t.Fatalf("History lost absence annotation: %v", item)
						}
					} else if item.Recovery != nil {
						t.Fatalf("unrelated History item was reconciled: %v", item)
					}
					if item.ItemID == "prior-call" {
						completion, ok := item.Completion.(threadfold.ToolCompletion)
						if !ok || !completion.Succeeded || item.Output == nil {
							t.Fatalf("earlier successful tool result changed: %v", item)
						}
					}
					if item.ItemID == "pending-call" && (item.Output != nil || item.Completion != nil) {
						t.Fatalf("unexecuted tool acquired a result: %v", item)
					}
				}
				newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
				if err != nil {
					t.Fatal(err)
				}
				conversation, err = store.Read(t.Context(), newOwner)
				if err != nil {
					t.Fatal(err)
				}
				afterRecords, err := conversation.Archive.Records()
				if err != nil {
					t.Fatal(err)
				}
				if len(afterRecords) != len(beforeRecords)+1 {
					t.Fatalf("recovery committed unexpected provider records: %v", afterRecords)
				}
				for index, record := range beforeRecords {
					if !bytes.Equal(record, afterRecords[index]) {
						t.Fatalf("recovery changed earlier context record %d", index)
					}
				}
				if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("late")}); !errors.As(err, new(*LostOwnershipError)) {
					t.Fatalf("late provider evidence was not fenced: %v", err)
				}
			})
		}
	}
}

func sampleRecoveryFixture(t *testing.T, api protocol.APIStyle, id, cut string, evidence *ResponseEvidence) (*sampleResult, error) {
	t.Helper()
	var events []any
	if api == protocol.APIStyleResponses {
		if cut != "no_items" {
			items := []responses.Item{
				{Type: responses.ItemReasoning, ID: id + "-reasoning", EncryptedContent: "fixture-opaque", Summary: json.RawMessage(`[{"type":"summary_text","text":"observed reasoning"}]`)},
				{Type: responses.ItemMessage, ID: id + "-text", Role: "assistant", Content: json.RawMessage(`[{"type":"output_text","text":"observed text"}]`)},
				{Type: responses.ItemFunctionCall, ID: id + "-tool", CallID: id + "-call", Name: "exec", Arguments: `{"code":"text(1)"}`},
			}
			for index, item := range items {
				events = append(events, responses.Event{Type: responses.EventOutputItemAdded, Item: &item})
				if index == 1 && cut == "during_text" {
					text := "observed prefix"
					events = append(events, responses.Event{Type: "response.output_text.delta", ItemID: item.ID, Delta: &text})
					break
				}
				events = append(events, responses.Event{Type: responses.EventOutputItemDone, Item: &item})
			}
			if cut == "complete" {
				events = append(events, map[string]any{"type": "response.completed", "response": map[string]any{"id": id, "status": "completed"}})
			}
		}
	} else {
		events = append(events, map[string]any{"type": "message_start", "message": map[string]any{
			"id": id, "role": "assistant", "content": []any{}, "usage": map[string]any{"input_tokens": 1, "output_tokens": 0},
		}})
		if cut != "no_items" {
			blocks := []map[string]any{
				{"type": "thinking", "thinking": "observed reasoning", "signature": "fixture-signature"},
				{"type": "text", "text": "observed text"},
				{"type": "tool_use", "id": id + "-call", "name": "exec", "input": map[string]any{"code": "text(1)"}},
			}
			for index, block := range blocks {
				events = append(events, map[string]any{"type": "content_block_start", "index": index, "content_block": block})
				if index == 1 && cut == "during_text" {
					events = append(events, map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "text_delta", "text": " prefix"}})
					break
				}
				events = append(events, map[string]any{"type": "content_block_stop", "index": index})
			}
			if cut == "complete" {
				events = append(events, map[string]any{"type": "message_delta", "delta": map[string]any{"stop_reason": "tool_use"}}, map[string]any{"type": "message_stop"})
			}
		}
	}
	var stream strings.Builder
	for _, event := range events {
		wire, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		fmt.Fprintf(&stream, "data: %s\n\n", wire)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, stream.String())
	}))
	defer server.Close()
	config := ModelConfig{API: api, Responses: responses.Config{Dialect: responses.DialectStandard}, AnthropicMessages: &anthropic.Config{MaxTokens: 1024}}
	adapter, err := adapterFor(config)
	if err != nil {
		t.Fatal(err)
	}
	connection := ConnectionConfig{BaseURL: server.URL, Auth: AuthConfig{Credential: "fixture-key"}}
	return adapter.Sample(t.Context(), server.Client(), connection, responses.Request{}, []byte(`{}`), evidence, nil, false)
}
