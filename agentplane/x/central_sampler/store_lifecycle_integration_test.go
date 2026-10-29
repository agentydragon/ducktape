package centralsampler

import (
	"bytes"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
)

func TestProtocolEventsDoNotLoadRetainedConversationItems(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `DROP TABLE sampler_conversation_item`); err != nil {
		t.Fatal(err)
	}
	if err := store.ProtocolEvent(t.Context(), owner, "operation", "executor", "received", map[string]any{"frame": "ok"}); err != nil {
		t.Fatalf("event write depended on loading the retained transcript: %v", err)
	}
}

func TestConversationTranscriptIsAppendOnlyAcrossInputAndCompaction(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	for _, input := range []string{"first input", "second input"} {
		if err := store.AppendInput(t.Context(), owner, input); err != nil {
			t.Fatal(err)
		}
	}
	readTranscript := func() ([][]byte, error) {
		rows, err := store.DB().QueryContext(t.Context(), `SELECT item FROM sampler_conversation_item
			WHERE conversation_id=$1 ORDER BY ordinal`, session.ConversationID)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		var items [][]byte
		for rows.Next() {
			var item []byte
			if err := rows.Scan(&item); err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		return items, rows.Err()
	}
	before, err := readTranscript()
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != 2 {
		t.Fatalf("transcript rows=%d, want 2", len(before))
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	replacement := []ResponsesItem{{Type: ResponsesItemCompaction, EncryptedContent: "opaque"}}
	intentID, err := store.BeginCompaction(t.Context(), owner, map[string]any{"kind": "compaction"}, conversation.Context())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.FinishCompaction(t.Context(), owner, intentID, map[string]any{"response": "complete"}, conversation.Context(), replacement); err != nil {
		t.Fatal(err)
	}
	after, err := readTranscript()
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != len(before) {
		t.Fatalf("compaction changed transcript row count: got %d, want %d", len(after), len(before))
	}
	for i := range before {
		if !bytes.Equal(after[i], before[i]) {
			t.Fatalf("compaction rewrote retained item %d: before=%s after=%s", i+1, before[i], after[i])
		}
	}
	conversation, err = store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	compacted := conversation.Context()
	if len(compacted) == 0 || compacted[len(compacted)-1].EncryptedContent != "opaque" {
		t.Fatalf("compacted context=%#v", compacted)
	}
}

func TestBlockedReasonNamesUnresolvedIntentInGetSession(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: "turn",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}},
	}, "fixture-account"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	intentID, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "turn", "effects_unknown"); err != nil {
		t.Fatal(err)
	}
	blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !blocked.Blocked || blocked.BlockedReason == nil {
		t.Fatalf("blocked session omitted reason: %+v", blocked)
	}
	if !bytes.Contains([]byte(*blocked.BlockedReason), []byte(intentID)) {
		t.Errorf("blocked reason %q does not name unresolved intent %q", *blocked.BlockedReason, intentID)
	}
	described, err := Describe(blocked)
	if err != nil {
		t.Fatal(err)
	}
	if described.GetBlockedReason() != *blocked.BlockedReason {
		t.Fatalf("GetSession response blocked_reason=%q, want %q", described.GetBlockedReason(), *blocked.BlockedReason)
	}
}
