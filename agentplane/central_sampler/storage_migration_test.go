package centralsampler

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestNativeContentMigrationRequiresMaintenanceAndStoppedWriters(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	// Simulate a crash after retirement committed and before Goose recorded success.
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=13`)
	if err := Migrate(t.Context(), store.DB()); err == nil || !strings.Contains(err.Error(), "--migrate-only") {
		t.Fatalf("normal startup conversion error=%v", err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := MigrateDuringMaintenance(t.Context(), store.DB()); err == nil || !strings.Contains(err.Error(), "leases") {
		t.Fatalf("maintenance with a live writer error=%v", err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if err := MigrateDuringMaintenance(t.Context(), store.DB()); err != nil {
		t.Fatalf("resume after retirement: %v", err)
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=13 AND is_applied`); count != 1 {
		t.Fatalf("successful migration history rows=%d", count)
	}
}

func TestNativeContentMigrationRefusesAmbiguousHistoricalProfiles(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	for _, intent := range []struct{ id, system string }{
		{"00000000-0000-0000-0000-000000000011", "first system"},
		{"00000000-0000-0000-0000-000000000012", "different system"},
	} {
		request := protocol.MustJSON(map[string]any{"kind": "sampling", "api": protocol.APIStyleAnthropicMessages, "body": map[string]any{"model": "fixture", "system": intent.system, "messages": []any{}}})
		migrationExec(t, store.DB(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,0,$3,'{}')`, intent.id, session.ConversationID, request)
	}
	if err := migrateContentStore(t.Context(), store.DB()); err == nil {
		t.Fatal("migration guessed a latest profile without dispatch evidence")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_intent WHERE request ? 'body'`); count != 2 {
		t.Fatalf("ambiguous legacy requests were rewritten: remaining=%d", count)
	}
}

func TestLegacyDispatchSequencesUseBoundedJournalOrder(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}

	for _, event := range []struct {
		id   string
		kind eventpb.ExecutionProgress_Kind
	}{
		{id: "intent-later", kind: eventpb.ExecutionProgress_KIND_SAMPLING},
		{id: "not-a-provider-intent", kind: eventpb.ExecutionProgress_KIND_CELL},
		{id: "intent-earlier", kind: eventpb.ExecutionProgress_KIND_COMPACTION},
	} {
		if err := store.ProtocolEvent(t.Context(), owner, event.id, event.kind,
			eventpb.ExecutionProgress_PHASE_DISPATCH, map[string]any{"test": "migration"}); err != nil {
			t.Fatal(err)
		}
	}

	tx, err := store.DB().BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback() }()

	dispatches, err := scanLegacyDispatchSequences(t.Context(), tx, session.ID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(dispatches) != 2 {
		t.Fatalf("dispatch sequences=%v, want only sampling and compaction evidence", dispatches)
	}
	first, firstOK := dispatches["intent-later"]
	if !firstOK {
		t.Fatalf("first provider dispatch is missing: %v", dispatches)
	}
	second, secondOK := dispatches["intent-earlier"]
	if !secondOK || second <= first {
		t.Fatalf("second provider dispatch sequence=%d (present=%v), must follow first sequence %d", second, secondOK, first)
	}
	if _, ok := dispatches["not-a-provider-intent"]; ok {
		t.Fatalf("non-provider boundary was included in dispatch map: %v", dispatches)
	}

	if _, err := legacyDispatchSequences(t.Context(), tx, session.ID); err != nil {
		t.Fatalf("default bounded scan: %v", err)
	}
}

func TestMigrateContentStoreResumesAfterCommittedArchivePrefix(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const sequence = 900000
	wantEntry := &eventlogpb.EventEntry{
		Cursor: sequence,
		Event: &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
			ItemId: "historical-event", Text: strings.Repeat("exact historical event bytes ", 300),
		}}},
	}
	wantJournal, err := proto.Marshal(wantEntry)
	if err != nil {
		t.Fatal("marshal historical journal entry:", err)
	}
	beforeEvents, err := store.Events(t.Context(), session.ID, sequence-1, 1)
	if err != nil {
		t.Fatal("read journal before migration:", err)
	}
	if len(beforeEvents) != 0 {
		t.Fatal("high sequence test event unexpectedly existed before insertion")
	}
	migrationExec(t, store.DB(), `INSERT INTO sampler_event(session_id,sequence,entry) VALUES($1,$2,$3)`,
		session.ID, sequence, wantJournal)
	beforeEvents, err = store.Events(t.Context(), session.ID, sequence-1, 1)
	if err != nil {
		t.Fatal("read inserted journal entry before migration:", err)
	}
	if len(beforeEvents) != 1 || !proto.Equal(beforeEvents[0], wantEntry) {
		t.Fatalf("pre-migration event export differs: got %v want %v", beforeEvents, wantEntry)
	}

	// Reconstruct the inline schema so this test exercises the same conversion path after the
	// baseline and index migrations have already run.
	migrationExec(t, store.DB(), `ALTER TABLE sampler_conversation ADD COLUMN context JSONB`)
	migrationExec(t, store.DB(), `CREATE TABLE sampler_conversation_item (
			conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
			ordinal BIGINT NOT NULL CHECK(ordinal>0), record JSONB NOT NULL,
			PRIMARY KEY(conversation_id,ordinal))`)
	migrationExec(t, store.DB(), `UPDATE sampler_conversation SET archive_context_id=NULL,archive_control=NULL WHERE id=$1`, session.ConversationID)
	first := []byte(`{"type":"message","role":"user","content":"first archive record"}`)
	unsupported := []byte(`{"role":"user","content":"bad legacy record has no type"}`)
	migrationExec(t, store.DB(), `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record)
		VALUES($1,1,$2::jsonb),($1,2,$3::jsonb)`, session.ConversationID, first, unsupported)

	if err := migrateContentStoreWithBatchSize(t.Context(), store.DB(), 1); err == nil {
		t.Fatal("migration succeeded despite unsupported legacy archive record")
	}
	var root string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT archive_context_id FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&root); err != nil {
		t.Fatal("read committed archive prefix:", err)
	}
	if root == "" {
		t.Fatal("first archive batch did not publish a root before the later invalid record")
	}
	if got := migrationCount(t, store.DB(), `SELECT item_count FROM sampler_context WHERE id=$1`, root); got != 1 {
		t.Fatalf("committed context prefix item count=%d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation_item WHERE conversation_id=$1`, session.ConversationID); got != 2 {
		t.Fatalf("legacy archive rows after failed conversion=%d, want 2", got)
	}
	var storedJournal []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, session.ID, sequence).Scan(&storedJournal); err != nil {
		t.Fatal("read physically stored journal event:", err)
	}
	if len(storedJournal) < 2 || !bytes.Equal(storedJournal[:2], []byte{0x1f, 0x8b}) {
		t.Fatal("large legacy journal entry was not compressed before archive conversion failed")
	}
	reader, err := gzip.NewReader(bytes.NewReader(storedJournal))
	if err != nil {
		t.Fatal("open compressed journal event:", err)
	}
	gotJournal, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal("inflate compressed journal event:", err)
	}
	if err := reader.Close(); err != nil {
		t.Fatal("close compressed journal event:", err)
	}
	if !bytes.Equal(gotJournal, wantJournal) {
		t.Fatal("physical journal compression changed bytes required for exact replay comparison")
	}
	afterEvents, err := store.Events(t.Context(), session.ID, sequence-1, 1)
	if err != nil {
		t.Fatal("read journal after migration:", err)
	}
	if len(afterEvents) != 1 || !proto.Equal(afterEvents[0], beforeEvents[0]) {
		t.Fatalf("post-migration event export differs from original: got %v want %v", afterEvents, beforeEvents)
	}

	// Fix only the unsupported row and rerun. The committed content prefix must be reused, not
	// duplicated, and the old inline table is retired only once the remaining suffix validates.
	second := []byte(`{"type":"message","role":"assistant","content":"second archive record"}`)
	migrationExec(t, store.DB(), `UPDATE sampler_conversation_item SET record=$2::jsonb
		WHERE conversation_id=$1 AND ordinal=2`, session.ConversationID, second)
	if err := migrateContentStoreWithBatchSize(t.Context(), store.DB(), 1); err != nil {
		t.Fatalf("resume native content migration: %v", err)
	}
	if exists, err := migrationTableExists(t.Context(), store.DB(), "sampler_conversation_item"); err != nil || exists {
		t.Fatalf("legacy conversation item table exists=%v, err=%v", exists, err)
	}
	if exists, err := migrationColumnExists(t.Context(), store.DB(), "sampler_conversation", "context"); err != nil || exists {
		t.Fatalf("legacy context column exists=%v, err=%v", exists, err)
	}
	var archiveRoot string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT archive_context_id FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&archiveRoot); err != nil {
		t.Fatal("read final archive root:", err)
	}
	tx, err := store.DB().BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	ids, err := newContentStore(t.Context(), tx).loadContext(archiveRoot)
	if err != nil {
		_ = tx.Rollback()
		t.Fatal("load final archive root:", err)
	}
	if len(ids) != 2 {
		_ = tx.Rollback()
		t.Fatalf("final archive context items=%d, want 2", len(ids))
	}
	records, err := newContentStore(t.Context(), tx).restoreRecords(protocol.APIStyleResponses, ids)
	if err != nil {
		_ = tx.Rollback()
		t.Fatal("restore final archive records:", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if !jsonEquivalent(records[0], first) || !jsonEquivalent(records[1], second) {
		t.Fatalf("restored archive records differ from legacy values: %s, %s", records[0], records[1])
	}
}

func TestMigrateContentStorePreservesProviderRequestsAndSignedContinuation(t *testing.T) {
	store := newTestStore(t)
	responsesSession := openSessionForStore(t, store, "")
	anthropicSession, err := openStoreSession(t, store, &samplerpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "fixture-account", protocol.APIStyleAnthropicMessages)
	if err != nil {
		t.Fatal("open Anthropic fixture session:", err)
	}

	// Restore the inline portion of a version-12 database. The migration must process these
	// roots exactly as it does before cutover, then retire the compatibility columns at success.
	migrationExec(t, store.DB(), `ALTER TABLE sampler_conversation ADD COLUMN context JSONB`)
	migrationExec(t, store.DB(), `CREATE TABLE sampler_conversation_item (
			conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
			ordinal BIGINT NOT NULL CHECK(ordinal>0), record JSONB NOT NULL,
			PRIMARY KEY(conversation_id,ordinal))`)
	migrationExec(t, store.DB(), `UPDATE sampler_conversation SET archive_context_id=NULL,archive_control=NULL`)

	// IDs sort opposite the actual dispatch order, and the compaction request has a shorter input
	// after context reset. Journal boundaries must remain the ordering source for both.
	earlierUUID, laterUUID := "ffffffff-ffff-ffff-ffff-ffffffffffff", "00000000-0000-0000-0000-000000000001"
	responsesOwner, err := store.Claim(t.Context(), responsesSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal("claim Responses session:", err)
	}
	for _, dispatched := range []struct {
		id   string
		kind eventpb.ExecutionProgress_Kind
	}{
		{id: earlierUUID, kind: eventpb.ExecutionProgress_KIND_SAMPLING},
		{id: laterUUID, kind: eventpb.ExecutionProgress_KIND_COMPACTION},
	} {
		if err := store.ProtocolEvent(t.Context(), responsesOwner, dispatched.id, dispatched.kind,
			eventpb.ExecutionProgress_PHASE_DISPATCH, map[string]any{"legacy": true}); err != nil {
			t.Fatal("append historical provider dispatch:", err)
		}
	}
	responseRequest := func(input string) json.RawMessage {
		body := json.RawMessage(`{"model":"fixture-model","instructions":"frozen instructions","input":` + input +
			`,"stream":true,"store":false,"include":["reasoning.encrypted_content"],"prompt_cache_key":"fixture-session",` +
			`"client_metadata":{"session_id":"fixture-session","thread_id":"fixture-thread","turn_id":"fixture-turn"},` +
			`"reasoning":{"effort":"high","summary":"auto","context":"all_turns"},"tools":[],"parallel_tool_calls":false,` +
			`"provider_extension":{"numeric_token":1e3}}`)
		return body
	}
	firstResponsesBody := responseRequest(`[{"type":"message","role":"user","content":"first"},{"type":"message","role":"assistant","content":"reply"}]`)
	compactionBody := responseRequest(`[{"type":"message","role":"user","content":"compacted prefix"}]`)
	addLegacyProviderIntent(t, store, responsesSession.ConversationID, earlierUUID, PendingIntentSampling,
		protocol.APIStyleResponses, firstResponsesBody, []byte(`{"status":"completed"}`))
	addLegacyProviderIntent(t, store, responsesSession.ConversationID, laterUUID, PendingIntentCompaction,
		protocol.APIStyleResponses, compactionBody, nil)

	const anthropicIntentID = "10000000-0000-0000-0000-000000000001"
	anthropicOwner, err := store.Claim(t.Context(), anthropicSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal("claim Anthropic session:", err)
	}
	if err := store.ProtocolEvent(t.Context(), anthropicOwner, anthropicIntentID,
		eventpb.ExecutionProgress_KIND_SAMPLING, eventpb.ExecutionProgress_PHASE_DISPATCH,
		map[string]any{"legacy": true}); err != nil {
		t.Fatal("append Anthropic dispatch:", err)
	}
	message := json.RawMessage(`{"id":"msg_signed","type":"message","role":"assistant","content":[` +
		`{"type":"thinking","thinking":"preserve signed reasoning","signature":"opaque-signature",` +
		`"provider_number":1e3},{"type":"text","text":"answer"}],"stop_reason":"end_turn",` +
		`"usage":{"input_tokens":4,"output_tokens":5},"provider_unknown":{"keep":"yes"}}`)
	anthropicBody := json.RawMessage(`{"model":"claude-legacy","max_tokens":1024,"stream":true,"system":"frozen system",` +
		`"tools":[{"name":"exec","description":"Run a command","input_schema":{"type":"object","properties":{}},` +
		`"provider_tool_number":1e3}],"messages":[{"role":"user","content":[{"type":"text","text":"question"}]},` +
		string(message) + `],"thinking":{"type":"enabled","budget_tokens":256},"output_config":{"effort":"high"},` +
		`"future_request_field":{"number":1e3}}`)
	addLegacyProviderIntent(t, store, anthropicSession.ConversationID, anthropicIntentID, PendingIntentSampling,
		protocol.APIStyleAnthropicMessages, anthropicBody, nil)
	activeContext := json.RawMessage(`{"messages":[` + string(message) + `],"pending_effort":"high"}`)
	migrationExec(t, store.DB(), `UPDATE sampler_conversation SET context=$2::jsonb WHERE id=$1`,
		anthropicSession.ConversationID, activeContext)

	if err := migrateContentStoreWithBatchSize(t.Context(), store.DB(), 1); err != nil {
		t.Fatalf("migrate historical provider content: %v", err)
	}
	firstStored, err := store.LoadProviderRequest(t.Context(), responsesSession.ID, "fixture-account", earlierUUID)
	if err != nil {
		t.Fatal("load historical Responses request:", err)
	}
	compactionStored, err := store.LoadProviderRequest(t.Context(), responsesSession.ID, "fixture-account", laterUUID)
	if err != nil {
		t.Fatal("load historical compaction request:", err)
	}
	anthropicStored, err := store.LoadProviderRequest(t.Context(), anthropicSession.ID, "fixture-account", anthropicIntentID)
	if err != nil {
		t.Fatal("load historical Anthropic request:", err)
	}
	if !jsonEquivalent(firstStored.Body, firstResponsesBody) || !jsonEquivalent(compactionStored.Body, compactionBody) ||
		!jsonEquivalent(anthropicStored.Body, anthropicBody) {
		t.Fatalf("restored historical provider bodies differ semantically: responses=%s compaction=%s anthropic=%s",
			firstStored.Body, compactionStored.Body, anthropicStored.Body)
	}
	for name, request := range map[string]*StoredProviderRequest{
		"Responses sampling":   firstStored,
		"Responses compaction": compactionStored,
		"Anthropic sampling":   anthropicStored,
	} {
		if request.Method != "POST" || request.URL != "https://provider.invalid/v1/messages" ||
			request.Headers["Content-Type"] != "application/json" {
			t.Errorf("%s route metadata changed: method=%q url=%q headers=%v", name, request.Method, request.URL, request.Headers)
		}
	}
	var firstPosition, compactionPosition int64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT position FROM sampler_model_request WHERE id=$1`, earlierUUID).Scan(&firstPosition); err != nil {
		t.Fatal(err)
	}
	if err := store.DB().QueryRowContext(t.Context(), `SELECT position FROM sampler_model_request WHERE id=$1`, laterUUID).Scan(&compactionPosition); err != nil {
		t.Fatal(err)
	}
	if firstPosition >= compactionPosition {
		t.Fatalf("journal order was replaced by UUID/context-length order: first=%d compaction=%d", firstPosition, compactionPosition)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_intent WHERE id IN ($1,$2) AND result IS NULL`, laterUUID, anthropicIntentID); got != 2 {
		t.Fatalf("pending provider results changed during migration: pending=%d, want 2", got)
	}
	var activeRoot string
	var activeControl []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT active_context_id,active_control FROM sampler_conversation WHERE id=$1`,
		anthropicSession.ConversationID).Scan(&activeRoot, &activeControl); err != nil {
		t.Fatal("read converted Anthropic active context:", err)
	}
	if activeRoot == "" || !jsonEquivalent(activeControl, []byte(`{"pending_effort":"high"}`)) {
		t.Fatalf("converted Anthropic active state lost its control: root=%q control=%s", activeRoot, activeControl)
	}
	tx, err := store.DB().BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	active, err := newContentStore(t.Context(), tx).loadNativeContext(protocol.APIStyleAnthropicMessages, activeRoot, activeControl)
	if err != nil {
		_ = tx.Rollback()
		t.Fatal("restore Anthropic signed active context:", err)
	}
	activeRecords, err := active.Records()
	if err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if len(activeRecords) != 1 || !jsonEquivalent(activeRecords[0], message) {
		t.Fatalf("Anthropic signed active record changed: %v", activeRecords)
	}
}

func addLegacyProviderIntent(t *testing.T, store *Store, conversationID, intentID string, kind PendingIntentKind,
	api protocol.APIStyle, body json.RawMessage, result json.RawMessage,
) {
	t.Helper()
	request, err := json.Marshal(map[string]any{
		"kind": kind, "api": api, "connection": "fixture-connection", "provider_model": "fixture-model",
		"method": "POST", "url": "https://provider.invalid/v1/messages", "headers": map[string]string{"Content-Type": "application/json"},
		"body": body, "wire_request": string(body),
	})
	if err != nil {
		t.Fatal("encode historical provider intent:", err)
	}
	var resultArg any
	if result != nil {
		resultArg = []byte(result)
	}
	migrationExec(t, store.DB(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result)
		VALUES($1,$2,0,$3::jsonb,$4::jsonb)`, intentID, conversationID, request, resultArg)
}
