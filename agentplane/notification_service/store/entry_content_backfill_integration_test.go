package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestBackfillEntryContentConvertsSourceVariantsAndClearsExpiredColumns(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-backfill"))
	requestID := uuid.New()
	messageID := uuid.New()
	caller := serviceAccountProto(model.ServiceAccount{Namespace: "caller-namespace", Name: "caller-sandbox"})
	operator := &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{
		Issuer: "https://operator.example", Subject: "operator-subject",
	}}}
	events := []model.EventIdentity{
		model.ActionsEvent{RequestID: requestID, Sequence: 1},
		model.ActionsEvent{RequestID: requestID, Sequence: 2},
		model.ActionsEvent{RequestID: requestID, Sequence: 3},
		model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4},
		model.GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5},
		model.MessageEvent{MessageID: messageID, Sender: model.ServiceAccount{Namespace: "sender-namespace", Name: "sender"}},
		model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest},
		model.GitHubObservationEvent{AppID: 1, RepositoryID: 2, Event: model.EventCheckRun, Sequence: 6},
	}
	actors := []*action_servicepb.Principal{operator, {Principal: &action_servicepb.Principal_Caller{Caller: caller}}, nil}
	for i, event := range events {
		payload := entryPayloadForTest(t, event)
		if action, ok := event.(model.ActionsEvent); ok {
			payload = actionContentPayload(t, action.Sequence, actors[i])
		}
		if _, ok := event.(model.GitHubMergeabilityEvent); ok {
			payload = `{"repository":"owner/repository","pullRequestNumber":3,"baseRef":"main","headSha":"head","baseSha":"base","previousMergeability":"UNKNOWN","mergeability":"CONFLICTING","observedAt":"2026-01-01T00:00:00.123456789Z","mergeCommit":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","headSha":"cccccccccccccccccccccccccccccccccccccccc"}}`
		}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payload)
		clearNativeEntryContent(t, f.db, subscription.InboxID, int64(i+1))
	}
	// Model an older cleanup replica: its JSON is already expired, but it left native fields behind below the
	// inbox's retained-prefix cursor. The backfill must clear native fields before the native reader release.
	expired := model.ActionsEvent{RequestID: requestID, Sequence: 99}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, expired, actionContentPayload(t, expired.Sequence, operator))
	expiredCursor := int64(len(events) + 1)
	f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, expiredCursor)
	f.exec(t, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, subscription.InboxID, expiredCursor+20)

	legacyPayloads := make(map[int64]sql.NullString, len(events))
	for i := range events {
		var legacy sql.NullString
		if err := f.db.QueryRowContext(t.Context(), `SELECT payload::text FROM entry WHERE inbox_id = $1 AND cursor = $2`,
			subscription.InboxID, i+1).Scan(&legacy); err != nil {
			t.Fatal(err)
		}
		legacyPayloads[int64(i+1)] = legacy
	}

	if err := BackfillEntryContent(t.Context(), f.db, 2); err != nil {
		t.Fatal(err)
	}
	for cursor, event := range events {
		var content entryContentStorage
		var payload sql.NullString
		destinations := append(content.destinations(), &payload)
		if err := f.db.QueryRowContext(t.Context(), `SELECT `+entryContentReadColumns+`, payload::text FROM entry
			WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, cursor+1).Scan(destinations...); err != nil {
			t.Fatal(err)
		}
		legacy := legacyPayloads[int64(cursor+1)]
		if payload.Valid != legacy.Valid || payload.String != legacy.String {
			t.Errorf("cursor %d legacy JSON changed during backfill", cursor+1)
		}
		switch event.(type) {
		case model.GitHubEvent, model.GitHubObservationEvent:
			if content.actionAt != nil || content.mergeabilityObservedAt != nil || content.messageBody != nil {
				t.Errorf("cursor %d unexpected native content: %+v", cursor+1, content)
			}
		default:
			reconstructed, err := content.payload(event)
			if err != nil {
				t.Fatalf("cursor %d reconstruct content: %v", cursor+1, err)
			}
			var got, want any
			if err := json.Unmarshal(reconstructed, &got); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(legacy.String), &want); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("cursor %d reconstructed content = %s, want %s", cursor+1, reconstructed, legacy.String)
			}
		}
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = $2
		AND payload IS NULL AND action_at IS NULL AND mergeability_observed_at IS NULL AND message_body IS NULL`, subscription.InboxID, expiredCursor); got != 1 {
		t.Fatalf("expired entry retained native content: %d", got)
	}
	var phase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_entry_content_backfill WHERE singleton`).Scan(&phase); err != nil {
		t.Fatal(err)
	}
	if phase != "backfilled" {
		t.Fatalf("backfill phase = %q, want backfilled", phase)
	}
}

func TestBackfillEntryContentBoundsBytesAndProcessesOversizedPayloadAlone(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-byte-bound"))
	paddingSizes := []int{400 << 10, 400 << 10, int(entryContentBackfillBatchBytes) + 1024, 128}
	requestID := uuid.New()
	for i, paddingSize := range paddingSizes {
		event := model.ActionsEvent{RequestID: requestID, Sequence: int64(i + 1)}
		actor := &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{
			Issuer: strings.Repeat("x", paddingSize), Subject: "subject",
		}}}
		payload := actionContentPayload(t, event.Sequence, actor)
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payload)
		clearNativeEntryContent(t, f.db, subscription.InboxID, int64(i+1))
	}
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done, err := entryContentBackfillBatch(t.Context(), conn, 16)
	if err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if done {
		conn.Close()
		t.Fatal("first bounded batch unexpectedly completed the scan")
	}
	var lastCursor int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_cursor FROM notification_entry_content_backfill WHERE singleton`).Scan(&lastCursor); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if lastCursor != 2 {
		conn.Close()
		t.Fatalf("byte-bounded first batch stopped at cursor %d, want 2", lastCursor)
	}
	firstBatchBytes := f.count(t, `SELECT COALESCE(sum(octet_length(payload::text)), 0)::int FROM entry
		WHERE inbox_id = $1 AND cursor <= 2 AND action_at IS NOT NULL`, subscription.InboxID)
	if int64(firstBatchBytes) > entryContentBackfillBatchBytes {
		conn.Close()
		t.Fatalf("first batch converted %d serialized bytes, limit is %d", firstBatchBytes, entryContentBackfillBatchBytes)
	}

	done, err = entryContentBackfillBatch(t.Context(), conn, 16)
	if err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if done {
		conn.Close()
		t.Fatal("oversized row batch unexpectedly completed the scan")
	}
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_cursor FROM notification_entry_content_backfill WHERE singleton`).Scan(&lastCursor); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if lastCursor != 3 {
		conn.Close()
		t.Fatalf("oversized payload batch stopped at cursor %d, want it processed alone at 3", lastCursor)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 4 AND action_at IS NOT NULL`, subscription.InboxID); got != 0 {
		conn.Close()
		t.Fatalf("row after oversized payload was converted in the same batch: %d", got)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if err := BackfillEntryContent(t.Context(), f.db, 16); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND action_at IS NOT NULL`, subscription.InboxID); got != len(paddingSizes) {
		t.Fatalf("native Action payloads after resuming = %d, want %d", got, len(paddingSizes))
	}
}

func TestBackfillEntryContentValidatesBeforePublishingCompletion(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-validate-before-complete"))
	requestID := uuid.New()
	first := model.ActionsEvent{RequestID: requestID, Sequence: 1}
	firstIdentity, err := json.Marshal(first)
	if err != nil {
		t.Fatal(err)
	}
	insertBackfilledEntry(t, f.db, subscription.InboxID, 1, firstIdentity, entryPayloadForTest(t, first))

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	for {
		done, err := entryContentBackfillBatch(t.Context(), conn, 1)
		if err != nil {
			conn.Close()
			t.Fatal(err)
		}
		if done {
			break
		}
	}
	var phase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_entry_content_backfill WHERE singleton`).Scan(&phase); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if phase != "scanned" {
		conn.Close()
		t.Fatalf("phase after scanning but before validation = %q, want scanned", phase)
	}

	// Model a writer outside the required gate that commits a legacy-only entry between the scan and validation.
	late := model.ActionsEvent{RequestID: requestID, Sequence: 2}
	lateIdentity, err := json.Marshal(late)
	if err != nil {
		conn.Close()
		t.Fatal(err)
	}
	insertBackfilledEntry(t, f.db, subscription.InboxID, 2, lateIdentity, entryPayloadForTest(t, late))
	err = validateEntryContentBackfill(t.Context(), conn)
	if err == nil || !strings.Contains(err.Error(), "native-source entries remain without native content") {
		conn.Close()
		t.Fatalf("incomplete validation error = %v", err)
	}
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_entry_content_backfill WHERE singleton`).Scan(&phase); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	if phase != "backfilling" {
		conn.Close()
		t.Fatalf("phase after failed validation = %q, want backfilling", phase)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}

	if err := BackfillEntryContent(t.Context(), f.db, 1); err != nil {
		t.Fatal(err)
	}
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_entry_content_backfill WHERE singleton`).Scan(&phase); err != nil {
		t.Fatal(err)
	}
	if phase != "backfilled" {
		t.Fatalf("phase after successful validation = %q, want backfilled", phase)
	}
}

func TestBackfillEntryContentDoesNotResurrectPayloadExpiredWhileWaitingForRowLock(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-expiry-race"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	clearNativeEntryContent(t, f.db, subscription.InboxID, 1)
	cleanupTx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := cleanupTx.ExecContext(t.Context(), `UPDATE entry SET payload = NULL
		WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID); err != nil {
		cleanupTx.Rollback()
		t.Fatal(err)
	}

	backfillDone := make(chan error, 1)
	go func() {
		backfillDone <- BackfillEntryContent(context.Background(), f.db, 1)
	}()
	waitCtx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	waiting := false
	for !waiting {
		if err := f.db.QueryRowContext(waitCtx, `SELECT EXISTS (
			SELECT 1 FROM pg_stat_activity
			WHERE datname = current_database() AND wait_event_type = 'Lock'
				AND query LIKE 'SELECT e.inbox_id, e.cursor,%'
		)`).Scan(&waiting); err != nil {
			cleanupTx.Rollback()
			t.Fatal(err)
		}
		if waiting {
			break
		}
		select {
		case err := <-backfillDone:
			cleanupTx.Rollback()
			t.Fatalf("backfill finished before observing the row lock: %v", err)
		case <-waitCtx.Done():
			cleanupTx.Rollback()
			t.Fatal("backfill did not wait for the cleanup row lock")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if err := cleanupTx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-backfillDone; err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1
		AND payload IS NULL AND action_at IS NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("raced expiry left both payload forms NULL = %d, want 1", got)
	}
}

func TestBackfillEntryContentFailureRetainsPayloadAndCursor(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-invalid"))
	requestID := uuid.New()
	valid := model.ActionsEvent{RequestID: requestID, Sequence: 1}
	invalid := model.ActionsEvent{RequestID: requestID, Sequence: 2}
	validIdentity, err := json.Marshal(valid)
	if err != nil {
		t.Fatal(err)
	}
	invalidIdentity, err := json.Marshal(invalid)
	if err != nil {
		t.Fatal(err)
	}
	insertBackfilledEntry(t, f.db, subscription.InboxID, 1, validIdentity, entryPayloadForTest(t, valid))
	insertBackfilledEntry(t, f.db, subscription.InboxID, 2, invalidIdentity,
		`{"sequence":2,"state":"decision_pending","at":"not-a-timestamp","actor":null}`)
	err = BackfillEntryContent(t.Context(), f.db, 1)
	if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("inbox %s entry 2", subscription.InboxID)) {
		t.Fatalf("invalid native content error = %v, want location inbox %s cursor 2", err, subscription.InboxID)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND action_at IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("first committed row native fields = %d, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2 AND payload IS NOT NULL AND action_at IS NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("failed row legacy evidence retained = %d, want 1", got)
	}
	var lastCursor int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_cursor FROM notification_entry_content_backfill WHERE singleton`).Scan(&lastCursor); err != nil {
		t.Fatal(err)
	}
	if lastCursor != 1 {
		t.Fatalf("progress cursor after conversion failure = %d, want 1", lastCursor)
	}
}

func TestEntryContentBackfillUsesPrimaryKeyForFirstAndLaterScansAndBulkUpdate(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("entry-content-plan"))
	requestID := uuid.New()
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at, identity_provider, actions_request_id, actions_sequence)
		SELECT $1, cursor, jsonb_build_object('provider', 'actions', 'request_id', $2::text, 'sequence', cursor),
			jsonb_build_object('sequence', cursor), now(), 'actions', $2::uuid, cursor
		FROM generate_series(1, 12000) AS series(cursor)`, subscription.InboxID, requestID)
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at, identity_provider, actions_request_id, actions_sequence, action_state, action_at)
		SELECT $1, cursor, jsonb_build_object('provider', 'actions', 'request_id', $2::text, 'sequence', cursor),
			NULL, now(), 'actions', $2::uuid, cursor, 1, now()
		FROM generate_series(12001, 12002) AS series(cursor)`, subscription.InboxID, requestID)
	f.exec(t, `ANALYZE entry`)
	var estimatedRows float64
	if err := f.db.QueryRowContext(t.Context(), `SELECT reltuples FROM pg_class WHERE oid = 'entry'::regclass`).Scan(&estimatedRows); err != nil {
		t.Fatal(err)
	}
	if estimatedRows < 10000 {
		t.Fatalf("plan fixture has %.0f estimated entry rows, want at least 10000", estimatedRows)
	}
	var laterInboxID uuid.UUID
	var laterCursor int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT inbox_id, cursor FROM entry
		WHERE inbox_id = $1 ORDER BY inbox_id, cursor OFFSET 6000 LIMIT 1`, subscription.InboxID).Scan(&laterInboxID, &laterCursor); err != nil {
		t.Fatal(err)
	}
	planTx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer planTx.Rollback()
	plans := []struct {
		name      string
		query     string
		args      []any
		allowSort bool
	}{
		{name: "first primary-key scan", query: entryContentBackfillFromStart, args: []any{entryContentBackfillBatchSize}},
		{name: "later primary-key scan", query: entryContentBackfillAfter, args: []any{laterInboxID, laterCursor, entryContentBackfillBatchSize}},
		{
			name: "selected content load", query: entryContentBackfillLoad,
			args: []any{pq.Array([]string{subscription.InboxID.String()}), pq.Array([]int64{6000, 6001})}, allowSort: true,
		},
		{
			name: "bulk expired cleanup", query: entryContentExpiredUpdate,
			args: []any{pq.Array([]string{subscription.InboxID.String()}), pq.Array([]int64{12001, 12002})},
		},
	}
	for _, check := range plans {
		plan, err := explainLines(t.Context(), planTx, check.query, check.args...)
		if err != nil {
			t.Fatalf("explain %s: %v", check.name, err)
		}
		if !strings.Contains(plan, "entry_pkey") || strings.Contains(plan, "Seq Scan") ||
			(!check.allowSort && strings.Contains(plan, "Sort")) {
			t.Errorf("%s plan must use entry_pkey without a full table scan or unbounded sort:\n%s", check.name, plan)
		}
	}
}

func actionContentPayload(t *testing.T, sequence int64, actor *action_servicepb.Principal) string {
	t.Helper()
	encoded, err := json.Marshal(ActionEvent{
		Sequence: sequence, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING,
		At: time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC), Actor: actor,
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func clearNativeEntryContent(t *testing.T, db *sql.DB, inboxID uuid.UUID, cursor int64) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `UPDATE entry SET action_state = NULL, action_at = NULL, action_actor_kind = NULL, action_actor_namespace = NULL, action_actor_name = NULL, action_actor_issuer = NULL, action_actor_subject = NULL, mergeability_repository = NULL, mergeability_base_ref = NULL, mergeability_head_sha = NULL, mergeability_base_sha = NULL, mergeability_previous = NULL, mergeability_current = NULL, mergeability_observed_at = NULL, mergeability_observed_at_submicro_nanos = NULL, merge_commit_sha = NULL, merge_commit_base_sha = NULL, merge_commit_head_sha = NULL, message_body = NULL, message_from_session = NULL
		WHERE inbox_id = $1 AND cursor = $2`, inboxID, cursor); err != nil {
		t.Fatal(err)
	}
}
