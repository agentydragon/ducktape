package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/entrystoragepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

func TestBackfillEntryContentConvertsSourceVariantsAndClearsExpiredBytes(t *testing.T) {
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
			payload = `{"repository":"owner/repository","pullRequestNumber":3,"baseRef":"main","headSha":"head","baseSha":"base","previousMergeability":"UNKNOWN","mergeability":"CONFLICTING","observedAt":"2026-01-01T00:00:00Z","mergeCommit":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","headSha":"cccccccccccccccccccccccccccccccccccccccc"}}`
		}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payload)
		if _, err := f.db.ExecContext(t.Context(), `UPDATE entry SET typed_payload = NULL
			WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1); err != nil {
			t.Fatal(err)
		}
	}
	// Model an older cleanup replica: its JSON is already expired, but it left typed bytes behind below the
	// inbox's retained-prefix cursor. The backfill must clear the physical bytes before the typed-reader release.
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
		var encoded []byte
		var payload sql.NullString
		if err := f.db.QueryRowContext(t.Context(), `SELECT typed_payload, payload::text FROM entry
			WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, cursor+1).Scan(&encoded, &payload); err != nil {
			t.Fatal(err)
		}
		if legacy := legacyPayloads[int64(cursor+1)]; payload.Valid != legacy.Valid || payload.String != legacy.String {
			t.Errorf("cursor %d legacy JSON after backfill = (%t, %q), want (%t, %q)",
				cursor+1, payload.Valid, payload.String, legacy.Valid, legacy.String)
		}
		if _, ok := event.(model.GitHubEvent); ok {
			if encoded != nil {
				t.Errorf("GitHub webhook typed payload = %x, want SQL NULL", encoded)
			}
			continue
		}
		if _, ok := event.(model.GitHubObservationEvent); ok {
			if encoded != nil {
				t.Errorf("GitHub observation typed payload = %x, want SQL NULL", encoded)
			}
			var observation map[string]any
			if err := json.Unmarshal([]byte(legacyPayloads[int64(cursor+1)].String), &observation); err != nil {
				t.Fatalf("decode native GitHub observation payload: %v", err)
			}
			if observation["repositoryId"] != float64(2) || observation["headSha"] != "0123456789abcdef0123456789abcdef01234567" {
				t.Errorf("GitHub observation payload changed during backfill: %s", legacyPayloads[int64(cursor+1)].String)
			}
			continue
		}
		var content entrystoragepb.EntryContent
		if err := proto.Unmarshal(encoded, &content); err != nil {
			t.Fatalf("cursor %d typed content is not protobuf: %v", cursor+1, err)
		}
		switch event := event.(type) {
		case model.ActionsEvent:
			action := content.GetAction()
			wantActor := actors[cursor]
			if action == nil || action.GetState() != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING ||
				action.GetAt() == nil || !action.GetAt().AsTime().Equal(time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)) ||
				!proto.Equal(action.GetActor(), wantActor) {
				t.Errorf("cursor %d Action content = %v, want actor %v", cursor+1, action, wantActor)
			}
		case model.GitHubMergeabilityEvent, model.GitHubMergeabilitySnapshotEvent:
			mergeability := content.GetMergeability()
			if mergeability == nil || mergeability.GetRepository() != "owner/repository" ||
				mergeability.GetBaseRef() != "main" || mergeability.GetHeadSha() != "head" || mergeability.GetBaseSha() != "base" ||
				mergeability.GetPreviousMergeability() != entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNKNOWN ||
				mergeability.GetMergeability() != entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_CONFLICTING {
				t.Errorf("cursor %d mergeability content = %v", cursor+1, mergeability)
			}
			if _, ok := event.(model.GitHubMergeabilityEvent); ok {
				mergeCommit := mergeability.GetMergeCommit()
				if mergeCommit == nil || mergeCommit.GetSha() != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ||
					mergeCommit.GetBaseSha() != "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ||
					mergeCommit.GetHeadSha() != "cccccccccccccccccccccccccccccccccccccccc" {
					t.Errorf("cursor %d merge commit content = %v", cursor+1, mergeCommit)
				}
			}
		case model.MessageEvent:
			message := content.GetMessage()
			if message == nil || message.GetBody() != "stored message" || message.FromSession == nil || *message.FromSession != "sender-session" {
				t.Errorf("cursor %d message content = %v", cursor+1, message)
			}
		default:
			t.Fatalf("unexpected event type %T", event)
		}
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = $2
		AND payload IS NULL AND typed_payload IS NULL`, subscription.InboxID, expiredCursor); got != 1 {
		t.Fatalf("expired entry has both payload representations NULL = %d, want 1", got)
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
		f.exec(t, `UPDATE entry SET typed_payload = NULL WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1)
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
		WHERE inbox_id = $1 AND cursor <= 2 AND typed_payload IS NOT NULL`, subscription.InboxID)
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
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 4 AND typed_payload IS NOT NULL`, subscription.InboxID); got != 0 {
		conn.Close()
		t.Fatalf("row after oversized payload was converted in the same batch: %d", got)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if err := BackfillEntryContent(t.Context(), f.db, 16); err != nil {
		t.Fatal(err)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND typed_payload IS NOT NULL`, subscription.InboxID); got != len(paddingSizes) {
		t.Fatalf("typed Action payloads after resuming = %d, want %d", got, len(paddingSizes))
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
	if err == nil || !strings.Contains(err.Error(), "typed-source entries remain without typed bytes") {
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
	f.exec(t, `UPDATE entry SET typed_payload = NULL WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
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
		AND payload IS NULL AND typed_payload IS NULL`, subscription.InboxID); got != 1 {
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
		t.Fatalf("invalid typed content error = %v, want location inbox %s cursor 2", err, subscription.InboxID)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND typed_payload IS NOT NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("first committed row typed bytes = %d, want 1", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 2 AND payload IS NOT NULL AND typed_payload IS NULL`, subscription.InboxID); got != 1 {
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
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at, identity_provider, actions_request_id, actions_sequence, typed_payload)
		SELECT $1, cursor, jsonb_build_object('provider', 'actions', 'request_id', $2::text, 'sequence', cursor),
			NULL, now(), 'actions', $2::uuid, cursor, decode('0a00', 'hex')
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
			name: "bulk typed-content update", query: entryContentBackfillUpdate,
			args: []any{
				pq.Array([]string{subscription.InboxID.String()}), pq.Array([]int64{6000, 6001}),
				pq.ByteaArray{[]byte{0x0a, 0x00}, []byte{0x0a, 0x00}},
			},
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
