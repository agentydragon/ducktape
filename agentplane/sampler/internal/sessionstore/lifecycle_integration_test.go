package sessionstore

import (
	"bytes"
	"errors"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func TestProtocolEventsDoNotLoadRetainedConversationItems(t *testing.T) {
	store, _, owner := claimedSession(t)
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_content RENAME TO unavailable_content`); err != nil {
		t.Fatal(err)
	}
	if err := store.ProtocolEvent(t.Context(), owner, "operation", eventpb.ExecutionProgress_KIND_EXECUTOR,
		eventpb.ExecutionProgress_PHASE_NOTIFICATION, map[string]any{"frame": "ok"}); err != nil {
		t.Fatalf("event write depended on loading the retained transcript: %v", err)
	}
}

func TestConversationTranscriptIsAppendOnlyAcrossInputAndCompaction(t *testing.T) {
	store, session, owner := claimedSession(t)
	if _, err := store.PrepareSamplingContext(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	for _, input := range []string{"first input", "second input"} {
		if err := store.AppendInput(t.Context(), owner, input); err != nil {
			t.Fatal(err)
		}
	}
	readTranscript := func() ([][]byte, error) {
		rows, err := store.DB().QueryContext(t.Context(), `WITH RECURSIVE transcript AS (
			SELECT c.* FROM sampler_context c JOIN sampler_conversation s ON s.transcript_context_id=c.id WHERE s.id=$1
			UNION ALL SELECT p.* FROM sampler_context p JOIN transcript c ON p.id=c.parent_id
		) SELECT content_id FROM transcript ORDER BY item_count`, session.ConversationID)
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
	if len(before) != 3 {
		t.Fatalf("transcript rows=%d, want 3", len(before))
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	replacement := responseContextForTest(t, []responses.Item{{Type: responses.ItemCompaction, EncryptedContent: "opaque"}})
	intentID, _, err := store.BeginCompaction(t.Context(), owner, IntentPayload{Kind: PendingIntentCompaction}, conversation.Context(), nil, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.FinishCompaction(t.Context(), owner, intentID, map[string]any{"response": "complete"}, conversation.Context(), replacement, nil); err != nil {
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
	compacted := testsupport.ResponsesItems(t, conversation.Context())
	if len(compacted) != 3 || compacted[1].EncryptedContent != "opaque" ||
		!bytes.Contains(compacted[2].Content, []byte("Active execution handles: ")) {
		t.Fatal("compacted context should contain the current environment, opaque summary and compaction snapshot")
	}
}

func TestBlockedReasonNamesUnresolvedIntentInGetSession(t *testing.T) {
	store, session, owner := claimedSession(t)
	admitInput(t, store, session.ID, "turn", "hello")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	intentID, _, err := store.Begin(t.Context(), owner, IntentPayload{Kind: PendingIntentSampling}, nil, providerAttemptTestPolicyPointer())
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

func TestStoreRejectsUnknownStatusesAndTurnResults(t *testing.T) {
	store, session, owner := claimedSession(t)
	if err := store.RegisterProcess(t.Context(), owner, "known-process", false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "known-process", nil, "not-a-status", nil, false); !errors.Is(err, protocol.ErrInvalid) {
		t.Errorf("ProcessResult with unknown status = %v, want ErrInvalid", err)
	}
	if err := store.CellResult(t.Context(), owner, "known-cell", "not-a-status"); !errors.Is(err, protocol.ErrInvalid) {
		t.Errorf("CellResult with unknown status = %v, want ErrInvalid", err)
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "turn", TurnCell); !errors.Is(err, protocol.ErrInvalid) {
		t.Errorf("FinishCommand with continuing turn result = %v, want ErrInvalid", err)
	}
	// The migration constraints hold even for writers that bypass the typed store API.
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_process SET status='not-a-status' WHERE id='known-process'`); err == nil {
		t.Error("database accepted an unknown process status")
	}
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_cell(id,conversation_id,generation,status) VALUES('c',$1,$2,'not-a-status')`,
		owner.ConversationID, owner.Generation); err == nil {
		t.Error("database accepted an unknown cell status")
	}
}
