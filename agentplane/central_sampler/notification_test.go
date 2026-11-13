package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
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
	sampling, _, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling}, nil)
	if err != nil {
		t.Fatal(err)
	}
	call := responses.Item{Type: responses.ItemCustomToolCall, CallID: "exec-call", Name: "exec", Input: "notify('progress')"}
	if err := store.Finish(t.Context(), owner, sampling, map[string]any{}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	cell, _, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentCell}, nil)
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
	invocation, err := conversation.Transcript.FindCall("exec-call")
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
	items := responsesItems(t, conversation.Transcript)
	if got, want := outputTexts(items), []string{"final", "progress", "after last step"}; !slices.Equal(got, want) {
		t.Fatalf("outputs=%q, want %q", got, want)
	}
	if err := conversation.Transcript.CheckSettled(); err != nil {
		t.Fatalf("notify outputs break call/output pairing: %v", err)
	}
	if count := queuedNotifications(t, store, session.ConversationID); count != 0 {
		t.Fatalf("turn end left %d notifications queued", count)
	}
}

func TestLostExecutorNoticeFollowsPendingToolResultForEachProvider(t *testing.T) {
	for _, incarnationChange := range []bool{false, true} {
		for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
			name := string(api) + "/lost-session"
			if incarnationChange {
				name = string(api) + "/incarnation-change"
			}
			t.Run(name, func(t *testing.T) {
				store := newTestStore(t)
				request := &samplerpb.CreateSessionRequest{
					SessionId: uuid.NewString(),
					Executor: &identitypb.SandboxDestination{
						Owner:  &identitypb.ServiceAccount{Namespace: "fixture", Name: "fixture-sandbox"},
						Target: &identitypb.EnvironmentTarget{Namespace: "fixture", Name: "fixture-sandbox", Uid: "sandbox-uid"},
					},
					ExpectedIncarnationUid: "original",
					Spec:                   &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
				}
				session, err := openStoreSession(t, store, request, "fixture-account", api)
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
				sampling, _, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling}, nil)
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
				noticeText := "retained exec-server session was definitively lost"
				if incarnationChange {
					noticeText = "execution Sandbox changed incarnation"
					err = store.RebindExecutorIncarnation(t.Context(), owner, "sandbox-uid", "original", "replacement", "")
				} else {
					err = store.RetireLostExecutorSession(t.Context(), owner, "retained-executor", "")
				}
				if err != nil {
					t.Fatal(err)
				}
				if count := queuedNotifications(t, store, session.ConversationID); count != 1 {
					t.Fatalf("loss notice queued=%d, want 1", count)
				}
				conversation, err := store.Read(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				if pending, err := conversation.Transcript.PendingCalls(); err != nil || len(pending) != 1 || pending[0].CallID != "call-1" {
					t.Fatalf("retiring executor changed pending model call: calls=%+v err=%v", pending, err)
				}
				beforeResult, err := conversation.Transcript.Records()
				if err != nil || len(beforeResult) == 0 {
					t.Fatalf("notice was appended before pending tool result: records=%d err=%v", len(beforeResult), err)
				}
				cell, _, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentCell}, nil)
				if err != nil {
					t.Fatal(err)
				}
				call, err := conversation.Transcript.FindCall("call-1")
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
				if err := conversation.Transcript.CheckSettled(); err != nil {
					t.Fatalf("loss notice broke provider tool-result ordering: %v", err)
				}
				if api == protocol.APIStyleResponses {
					items := responsesItems(t, conversation.Transcript)
					callIndex := -1
					for i, item := range items {
						if item.CallID == "call-1" && item.Type == responses.ItemCustomToolCall {
							callIndex = i
							break
						}
					}
					if callIndex < 0 || len(items) < callIndex+3 || !items[callIndex+1].IsToolOutput() || items[callIndex+1].Output[0].Text != "session was lost; operation not dispatched" ||
						!items[callIndex+2].IsToolOutput() || !strings.Contains(items[callIndex+2].Output[0].Text, noticeText) {
						t.Fatalf("Responses notice ordering is invalid: %+v", items)
					}
				} else {
					context := conversation.Transcript.(*anthropic.Context)
					wireMessages, err := context.WireMessages()
					if err != nil {
						t.Fatalf("Anthropic transcript is not valid provider input: %v", err)
					}
					var result, notice struct {
						Role    string `json:"role"`
						Content []struct {
							Type      string `json:"type"`
							ToolUseID string `json:"tool_use_id"`
							Text      string `json:"text"`
						} `json:"content"`
					}
					transcriptRecords, err := conversation.Transcript.Records()
					if err != nil || len(transcriptRecords) < 2 || len(wireMessages) != len(transcriptRecords) {
						t.Fatalf("Anthropic records=%d wire messages=%d err=%v", len(transcriptRecords), len(wireMessages), err)
					}
					if err := json.Unmarshal(transcriptRecords[len(transcriptRecords)-2], &result); err != nil {
						t.Fatal(err)
					}
					if err := json.Unmarshal(transcriptRecords[len(transcriptRecords)-1], &notice); err != nil {
						t.Fatal(err)
					}
					if result.Role != "user" || len(result.Content) != 1 || result.Content[0].Type != "tool_result" || result.Content[0].ToolUseID != "call-1" ||
						notice.Role != "user" || len(notice.Content) != 1 || notice.Content[0].Type != "text" || !strings.Contains(notice.Content[0].Text, noticeText) {
						t.Fatalf("Anthropic tool-result/notice ordering is invalid: result=%+v notice=%+v", result, notice)
					}
				}
			})
		}
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
			if texts := outputTexts(responsesItems(t, conversation.Transcript)); len(texts) != 0 {
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

	if err := peer.sendNotification(1, code.ID(), "cell-1", "between turns"); err != nil {
		t.Fatal(err)
	}
	assertDelegateResponse(t, receivePeerFrame(t, peer), 1, "error")
	if count := queuedNotifications(t, store, session.ConversationID); count != 0 {
		t.Fatalf("rejected notification was queued: %d", count)
	}

	startTurnForStore(t, store, session, owner, "turn")
	if err := peer.sendNotification(2, code.ID(), "cell-1", "progress"); err != nil {
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
