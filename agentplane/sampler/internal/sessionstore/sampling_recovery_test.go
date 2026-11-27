package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"slices"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/protobuf/proto"
)

func TestSamplingRecoveryProjectionMatchesJournalAcrossOwnerChange(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	session := openSessionForStore(t, store, "")
	owner := claim(t, store, session)
	const commandID = "indexed-sampling-command"
	admitInput(t, store, session.ID, commandID, "Start")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	partialID := "indexed-partial-item"
	largeFrame := bytes.Repeat([]byte("x"), BoundaryBytes)
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: time.Minute}).Record(t.Context(),
		protocol.WireBytes{Bytes: largeFrame}, true,
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: partialID}}}); err != nil {
		t.Fatal(err)
	}

	// An unrelated boundary and a linked ItemStarted between this attempt's evidence
	// must not enter the attempt-keyed projection or the recovery report.
	if err := appendUnrelatedSamplingStartedItem(t, store, session.ID); err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("unstarted provider frame")}, false); err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("duplicate item observation")}, false,
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: partialID}}}); err != nil {
		t.Fatal(err)
	}

	var turnID sql.NullString
	var dispatchSequence sql.NullInt64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT dispatch_turn_id,dispatch_sequence FROM sampler_intent WHERE id=$1`, intentID).
		Scan(&turnID, &dispatchSequence); err != nil {
		t.Fatal(err)
	}
	if !turnID.Valid || turnID.String != commandID || !dispatchSequence.Valid {
		t.Fatalf("dispatch projection turn=%v sequence=%v", turnID, dispatchSequence)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_intent_source WHERE intent_id=$1`, intentID); got != 3 {
		t.Fatalf("source-linked boundary fragments=%d, want the two fragmented and one duplicate-item evidence sources", got)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_intent_started_item WHERE intent_id=$1`, intentID); got != 1 {
		t.Fatalf("deduplicated started items=%d, want 1", got)
	}

	expireLease(t, store, session)
	newOwner := claim(t, store, session)
	if newOwner.Generation <= owner.Generation {
		t.Fatalf("ownership generation did not advance: old=%d new=%d", owner.Generation, newOwner.Generation)
	}
	store = reopenProviderAttemptStore(t, store, dsn)
	indexed := readSamplingReconciliation(t, store, session.ID, intentID)
	if got := indexed.GetConversationReconciled(); got == nil || got.GetTurnId() != commandID || len(got.GetItems()) != 1 ||
		got.GetItems()[0].GetItemId() != partialID || got.GetItems()[0].GetDisposition() != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT {
		t.Fatalf("indexed recovery report=%+v", got)
	}
	if slices.ContainsFunc(indexed.GetConversationReconciled().GetItems(), func(item *eventpb.ItemRecovery) bool {
		return item.GetItemId() == "unrelated-started-item"
	}) {
		t.Fatalf("unrelated started item leaked into indexed report: %+v", indexed)
	}

	// Removing both physical journal tables inside this transaction proves the complete
	// projection answers without reading or decoding an event block.
	var noJournalRead *eventpb.Event
	if err := store.transaction(t.Context(), "test.sampling_recovery_without_journal", func(ctx context.Context, tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `ALTER TABLE sampler_event RENAME TO sampler_event_sampling_recovery_scan_disabled`); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `ALTER TABLE sampler_event_block RENAME TO sampler_event_block_sampling_recovery_scan_disabled`); err != nil {
			return err
		}
		_, lockedSession, err := lockSession(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		noJournalRead, err = samplingReconciliation(ctx, tx, lockedSession, intentID)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `ALTER TABLE sampler_event_block_sampling_recovery_scan_disabled RENAME TO sampler_event_block`); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `ALTER TABLE sampler_event_sampling_recovery_scan_disabled RENAME TO sampler_event`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(indexed, noJournalRead) {
		t.Fatalf("report changed when journal tables were unavailable:\nindexed=%v\nno-journal=%v", indexed, noJournalRead)
	}

	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET dispatch_turn_id=NULL,dispatch_sequence=NULL WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
	journalFallback := readSamplingReconciliation(t, store, session.ID, intentID)
	if !proto.Equal(indexed, journalFallback) {
		t.Fatalf("indexed report differs from legacy journal reconstruction:\nindexed=%v\njournal=%v", indexed, journalFallback)
	}
}

func TestLegacySamplingJournalAppendInvalidatesProjectionAndSettlementCleansIt(t *testing.T) {
	store, session, owner := claimedSession(t)
	const commandID = "legacy-sampling-writer-command"
	admitInput(t, store, session.ID, commandID, "Start")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: time.Minute}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("partial provider frame")}, true,
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "legacy-partial-item"}}}); err != nil {
		t.Fatal(err)
	}
	indexed := readSamplingReconciliation(t, store, session.ID, intentID)

	// This transaction stands in for an older binary: it appends a valid unrelated event
	// without setting the migration-24 writer flag. The trigger must make this attempt fall
	// back to the authoritative journal before the new owner constructs a report.
	if err := store.transaction(t.Context(), "test.legacy_sampler_append", func(ctx context.Context, tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SELECT set_config('agentplane.sampler_indexed_recovery_writer', 'off', TRUE)`); err != nil {
			return err
		}
		_, lockedSession, err := lockSession(ctx, tx, session.ID)
		if err != nil {
			return err
		}
		_, err = store.appendEvent(ctx, tx, lockedSession, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{
			BoundaryRecord: &eventpb.BoundaryRecord{
				Boundary: "sampling", Direction: "received", CorrelationId: "unrelated-legacy-operation",
				Payload: []byte(`{"unrelated":true}`), MediaType: "application/json",
			},
		}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	var turnID sql.NullString
	var dispatchSequence sql.NullInt64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT dispatch_turn_id,dispatch_sequence FROM sampler_intent WHERE id=$1`, intentID).
		Scan(&turnID, &dispatchSequence); err != nil {
		t.Fatal(err)
	}
	if turnID.Valid || dispatchSequence.Valid {
		t.Fatalf("legacy append left a complete marker: turn=%v sequence=%v", turnID, dispatchSequence)
	}
	fallback := readSamplingReconciliation(t, store, session.ID, intentID)
	if !proto.Equal(indexed, fallback) {
		t.Fatalf("legacy-writer fallback differs from indexed report:\nindexed=%v\nfallback=%v", indexed, fallback)
	}

	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptPermanent, Reason: "interrupted sampling attempt",
	}); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"sampler_intent_source", "sampler_intent_started_item"} {
		if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM `+table+` WHERE intent_id=$1`, intentID); got != 0 {
			t.Fatalf("settled attempt retained %s projection rows: %d", table, got)
		}
	}
	var projectionCleared bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT dispatch_turn_id IS NULL AND dispatch_sequence IS NULL FROM sampler_intent WHERE id=$1`, intentID).
		Scan(&projectionCleared); err != nil {
		t.Fatal(err)
	}
	if !projectionCleared {
		t.Fatal("settled attempt retained its dispatch projection")
	}
}

func TestInterruptSettlementCleansSamplingRecoveryProjection(t *testing.T) {
	store, session, owner := claimedSession(t)
	const commandID = "interrupted-sampling-command"
	admitInput(t, store, session.ID, commandID, "Start")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	intentID, _, err := store.begin(t.Context(), owner, IntentPayload{Kind: PendingIntentSampling}, nil, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("partial provider response")}, false,
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "interrupted-item"}}}); err != nil {
		t.Fatal(err)
	}
	requestID, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AcknowledgeInterrupt(t.Context(), owner, []string{requestID}, map[string]any{
		"cleanup_complete": true,
		"status":           "interrupted",
	}); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"sampler_intent_source", "sampler_intent_started_item"} {
		if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM `+table+` WHERE intent_id=$1`, intentID); got != 0 {
			t.Fatalf("interrupted attempt retained %s projection rows: %d", table, got)
		}
	}
	var projectionCleared bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT dispatch_turn_id IS NULL AND dispatch_sequence IS NULL FROM sampler_intent WHERE id=$1`, intentID).
		Scan(&projectionCleared); err != nil {
		t.Fatal(err)
	}
	if !projectionCleared {
		t.Fatal("interrupted attempt retained its dispatch projection")
	}
}

func appendUnrelatedSamplingStartedItem(t *testing.T, store *Store, sessionID string) error {
	t.Helper()
	return store.transaction(t.Context(), "test.append_unrelated_sampling_item", func(ctx context.Context, tx *sql.Tx) error {
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		boundary, err := store.appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{
			BoundaryRecord: &eventpb.BoundaryRecord{
				Boundary: "sampling", Direction: "received", CorrelationId: "unrelated-operation",
				Payload: []byte(`{"unrelated":true}`), MediaType: "application/json",
			},
		}})
		if err != nil {
			return err
		}
		_, err = store.appendEvent(ctx, tx, session, &eventpb.Event{
			SourceSequences: []uint64{boundary.GetOrigin().GetSequence()},
			Observation:     &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "unrelated-started-item"}},
		})
		return err
	})
}

func readSamplingReconciliation(t *testing.T, store *Store, sessionID, intentID string) *eventpb.Event {
	t.Helper()
	var report *eventpb.Event
	if err := store.transaction(t.Context(), "test.read_sampling_reconciliation", func(ctx context.Context, tx *sql.Tx) error {
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		report, err = samplingReconciliation(ctx, tx, session, intentID)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	return report
}
