package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
)

func startTurnForStore(t *testing.T, store *Store, session *Session, owner Owner, commandID string) {
	t.Helper()
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: commandID}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	if command, err := store.NextCommand(t.Context(), owner, session.ID); err != nil || command == nil || command.ID != commandID {
		t.Fatalf("turn command=%v err=%v", command, err)
	}
}

func queuedNotifications(t *testing.T, store *Store, conversationID string) int {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_notification WHERE conversation_id=$1`, conversationID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func outputTexts(items []responses.Item) []string {
	var texts []string
	for _, item := range items {
		if item.Type == responses.ItemCustomToolOutput {
			texts = append(texts, item.Output[0].Text)
		}
	}
	return texts
}

func TestNotificationFollowsStepOutputsAndTurnEnd(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "idle"); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("notification without an active turn: %v", err)
	}
	startTurnForStore(t, store, session, owner, "turn")
	sampling, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
	if err != nil {
		t.Fatal(err)
	}
	call := responses.Item{Type: responses.ItemCustomToolCall, CallID: "exec-call", Name: "exec", Input: "notify('progress')"}
	if err := store.Finish(t.Context(), owner, sampling, map[string]any{}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	cell, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentCell})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "progress"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DrainNotifications(t.Context(), owner); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("drain before the step's own output was recorded: %v", err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	invocation, err := conversation.Archive.FindCall("exec-call")
	if err != nil {
		t.Fatal(err)
	}
	if err := store.FinishTool(t.Context(), owner, cell, map[string]any{}, invocation, []protocol.ContentPart{{Type: "input_text", Text: "final"}}); err != nil {
		t.Fatal(err)
	}
	if drained, err := store.DrainNotifications(t.Context(), owner); err != nil || !drained {
		t.Fatalf("drained=%v err=%v", drained, err)
	}
	if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "after last step"); err != nil {
		t.Fatal(err)
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "turn", "done"); err != nil {
		t.Fatal(err)
	}
	conversation, err = store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	items := responsesItems(t, conversation.Archive)
	if got, want := outputTexts(items), []string{"final", "progress", "after last step"}; !slices.Equal(got, want) {
		t.Fatalf("outputs=%q, want %q", got, want)
	}
	if err := conversation.Archive.CheckSettled(); err != nil {
		t.Fatalf("notify outputs break call/output pairing: %v", err)
	}
	if count := queuedNotifications(t, store, session.ConversationID); count != 0 {
		t.Fatalf("turn end left %d notifications queued", count)
	}
}

func TestLostExecutorNoticeFollowsPendingToolResultForEachProvider(t *testing.T) {
	for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
		t.Run(string(api), func(t *testing.T) {
			store := newTestStore(t)
			request := &samplerpb.OpenSessionRequest{
				SessionId: uuid.NewString(),
				Spec:      &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model"},
			}
			session, err := store.OpenSession(t.Context(), request, "fixture-account", api)
			if err != nil {
				t.Fatal(err)
			}
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
				t.Fatal(err)
			}
			startTurnForStore(t, store, session, owner, "turn")
			sampling, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
			if err != nil {
				t.Fatal(err)
			}
			var records []json.RawMessage
			if api == protocol.APIStyleResponses {
				call := responses.Item{Type: responses.ItemCustomToolCall, CallID: "call-1", Name: "exec", Input: "text(1)", ID: "item-1"}
				records = nativeResponsesRecords(t, call)
			} else {
				records = []json.RawMessage{json.RawMessage(`{"id":"msg-1","type":"message","role":"assistant","content":[{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use","usage":{"input_tokens":1,"output_tokens":1}}`)}
			}
			if err := store.Finish(t.Context(), owner, sampling, map[string]any{"id": "response"}, records, nil); err != nil {
				t.Fatal(err)
			}
			if err := store.RetireLostExecutorSession(t.Context(), owner, "retained-executor", ""); err != nil {
				t.Fatal(err)
			}
			if count := queuedNotifications(t, store, session.ConversationID); count != 1 {
				t.Fatalf("loss notice queued=%d, want 1", count)
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if pending, err := conversation.Archive.PendingCalls(); err != nil || len(pending) != 1 || pending[0].CallID != "call-1" {
				t.Fatalf("retiring executor changed pending model call: calls=%+v err=%v", pending, err)
			}
			beforeResult, err := conversation.Archive.Records()
			if err != nil || len(beforeResult) == 0 {
				t.Fatalf("notice was appended before pending tool result: records=%d err=%v", len(beforeResult), err)
			}
			cell, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentCell})
			if err != nil {
				t.Fatal(err)
			}
			call, err := conversation.Archive.FindCall("call-1")
			if err != nil {
				t.Fatal(err)
			}
			if err := store.FinishTool(t.Context(), owner, cell, map[string]any{}, call, []protocol.ContentPart{{Type: "input_text", Text: "session was lost; operation not dispatched"}}); err != nil {
				t.Fatal(err)
			}
			if drained, err := store.DrainNotifications(t.Context(), owner); err != nil || !drained {
				t.Fatalf("loss notice drained=%v err=%v", drained, err)
			}
			conversation, err = store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if err := conversation.Archive.CheckSettled(); err != nil {
				t.Fatalf("loss notice broke provider tool-result ordering: %v", err)
			}
			if api == protocol.APIStyleResponses {
				items := responsesItems(t, conversation.Archive)
				callIndex := -1
				for i, item := range items {
					if item.CallID == "call-1" && item.Type == responses.ItemCustomToolCall {
						callIndex = i
						break
					}
				}
				if callIndex < 0 || len(items) < callIndex+3 || !items[callIndex+1].IsToolOutput() || items[callIndex+1].Output[0].Text != "session was lost; operation not dispatched" ||
					!items[callIndex+2].IsToolOutput() || !strings.Contains(items[callIndex+2].Output[0].Text, "retained exec-server session was definitively lost") {
					t.Fatalf("Responses notice ordering is invalid: %+v", items)
				}
			} else {
				context := conversation.Archive.(*anthropic.Context)
				wireMessages, err := context.WireMessages()
				if err != nil {
					t.Fatalf("Anthropic archive is not valid provider input: %v", err)
				}
				var result, notice struct {
					Role    string `json:"role"`
					Content []struct {
						Type      string `json:"type"`
						ToolUseID string `json:"tool_use_id"`
						Text      string `json:"text"`
					} `json:"content"`
				}
				archiveRecords, err := conversation.Archive.Records()
				if err != nil || len(archiveRecords) < 2 || len(wireMessages) != len(archiveRecords) {
					t.Fatalf("Anthropic records=%d wire messages=%d err=%v", len(archiveRecords), len(wireMessages), err)
				}
				if err := json.Unmarshal(archiveRecords[len(archiveRecords)-2], &result); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(archiveRecords[len(archiveRecords)-1], &notice); err != nil {
					t.Fatal(err)
				}
				if result.Role != "user" || len(result.Content) != 1 || result.Content[0].Type != "tool_result" || result.Content[0].ToolUseID != "call-1" ||
					notice.Role != "user" || len(notice.Content) != 1 || notice.Content[0].Type != "text" || !strings.Contains(notice.Content[0].Text, "retained exec-server session was definitively lost") {
					t.Fatalf("Anthropic tool-result/notice ordering is invalid: result=%+v notice=%+v", result, notice)
				}
			}
		})
	}
}

func TestNotificationIsDiscardedWhenTurnDoesNotComplete(t *testing.T) {
	for _, ending := range []string{"interrupted", "effects_unknown", "failed"} {
		t.Run(ending, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			startTurnForStore(t, store, session, owner, "turn")
			if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "discarded"); err != nil {
				t.Fatal(err)
			}
			if ending == "failed" {
				commandID := "turn"
				err = store.FailExecution(t.Context(), owner, session.ID, &commandID, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "fixture failure"}})
			} else {
				err = store.FinishCommand(t.Context(), owner, session.ID, "turn", TurnResult(ending))
			}
			if err != nil {
				t.Fatal(err)
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if texts := outputTexts(responsesItems(t, conversation.Archive)); len(texts) != 0 {
				t.Fatalf("aborted turn recorded notify outputs %q", texts)
			}
			if count := queuedNotifications(t, store, session.ConversationID); count != 0 {
				t.Fatalf("aborted turn left %d notifications queued", count)
			}
		})
	}
}

func TestCodeModeAcknowledgesNotificationOnlyAfterDurableQueue(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &Executor{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = code.Detach(ctx)
	})
	peer := peers()[0]

	if err := peer.sendNotification(1, code.sessionID, "cell-1", "between turns"); err != nil {
		t.Fatal(err)
	}
	assertDelegateResponse(t, receivePeerFrame(t, peer), 1, "error")
	if count := queuedNotifications(t, store, session.ConversationID); count != 0 {
		t.Fatalf("rejected notification was queued: %d", count)
	}

	startTurnForStore(t, store, session, owner, "turn")
	if err := peer.sendNotification(2, code.sessionID, "cell-1", "progress"); err != nil {
		t.Fatal(err)
	}
	assertDelegateResponse(t, receivePeerFrame(t, peer), 2, "ok")
	if count := queuedNotifications(t, store, session.ConversationID); count != 1 {
		t.Fatalf("acknowledged notification is not durably queued: count=%d", count)
	}
	if code.Err() != nil {
		t.Fatalf("notification failed the binding: %v", code.Err())
	}
}
