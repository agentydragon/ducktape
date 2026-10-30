package centralsampler

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
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

func outputTexts(items []ResponsesItem) []string {
	var texts []string
	for _, item := range items {
		if item.Type == ResponsesItemCustomToolOutput {
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
	if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "idle"); !errors.Is(err, ErrPrecondition) {
		t.Fatalf("notification without an active turn: %v", err)
	}
	startTurnForStore(t, store, session, owner, "turn")
	sampling, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	call := ResponsesItem{Type: ResponsesItemCustomToolCall, CallID: "exec-call", Name: "exec", Input: "notify('progress')"}
	if err := store.Finish(t.Context(), owner, sampling, map[string]any{}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	cell, err := store.Begin(t.Context(), owner, map[string]any{"kind": "cell"})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.QueueNotification(t.Context(), owner, "exec-call", "cell", "progress"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DrainNotifications(t.Context(), owner); !errors.Is(err, ErrPrecondition) {
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
	if err := store.FinishTool(t.Context(), owner, cell, map[string]any{}, invocation, []ContentPart{{Type: "input_text", Text: "final"}}); err != nil {
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
				err = store.FailExecution(t.Context(), owner, session.ID, &commandID, &KnownFailureError{Evidence: map[string]any{"reason": "fixture failure"}})
			} else {
				err = store.FinishCommand(t.Context(), owner, session.ID, "turn", ending)
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
