package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/google/uuid"
)

func TestNativeMigrationDoesNotLockServingConversationOnRepeatedStartup(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	serving, err := store.DB().BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer serving.Rollback()
	if _, err := serving.ExecContext(t.Context(), `UPDATE sampler_conversation SET stopped=stopped WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	if err := Migrate(ctx, store.DB(), nil); err != nil {
		t.Fatalf("repeated initializer blocked a serving conversation: %v", err)
	}
}

func migrationRaw(value any) json.RawMessage {
	encoded, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return encoded
}

func anthropicEnvelope(message json.RawMessage) json.RawMessage {
	return migrationRaw(map[string]json.RawMessage{"type": migrationRaw("anthropic_message"), "content": message})
}

func installLegacyConversation(t *testing.T, store *Store, conversationID string, archive, active []json.RawMessage) {
	t.Helper()
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_conversation DROP CONSTRAINT IF EXISTS sampler_conversation_api_check`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_conversation ALTER COLUMN api DROP NOT NULL`); err != nil {
		t.Fatal(err)
	}
	contextWire := any(nil)
	if active != nil {
		contextWire = migrationRaw(active)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET api=NULL,context=$2::jsonb WHERE id=$1`, conversationID, contextWire); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `DELETE FROM sampler_conversation_item WHERE conversation_id=$1`, conversationID); err != nil {
		t.Fatal(err)
	}
	for i, record := range archive {
		if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record) VALUES($1,$2,$3::jsonb)`,
			conversationID, i+1, []byte(record)); err != nil {
			t.Fatal(err)
		}
	}
}

func addMigrationIntent(t *testing.T, store *Store, conversationID, api string) (string, string) {
	t.Helper()
	id := uuid.NewString()
	request := migrationRaw(map[string]any{"kind": "sampling", "api": api, "body": map[string]any{"prompt": "preserve me"}})
	result := migrationRaw(map[string]any{"status": "completed", "provider_result": "preserve me"})
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,0,$3::jsonb,$4::jsonb)`,
		id, conversationID, []byte(request), []byte(result)); err != nil {
		t.Fatal(err)
	}
	return id, string(request) + "\n" + string(result)
}

func addLegacyIntentWithoutAPI(t *testing.T, store *Store, conversationID string) {
	t.Helper()
	id := uuid.NewString()
	request := migrationRaw(map[string]any{"kind": "sampling", "body": map[string]any{"prompt": "preserve me"}})
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,0,$3::jsonb,NULL)`, id, conversationID, []byte(request)); err != nil {
		t.Fatal(err)
	}
}

func legacyRecordSnapshot(t *testing.T, store *Store, conversationID string) []string {
	t.Helper()
	rows, err := store.DB().QueryContext(t.Context(), `SELECT record::text FROM sampler_conversation_item WHERE conversation_id=$1 ORDER BY ordinal`, conversationID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var records []string
	for rows.Next() {
		var record string
		if err := rows.Scan(&record); err != nil {
			t.Fatal(err)
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return records
}

func TestConvertLegacyAnthropicTranscriptChecksProjectionAndPreservesMessages(t *testing.T) {
	native := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"thinking","thinking":"keep this thought","signature":"sig-think"},{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use","usage":{"input_tokens":7}}`)
	records := []json.RawMessage{
		migrationRaw(map[string]any{"type": "message", "role": "user", "content": "run it"}),
		anthropicEnvelope(native),
		migrationRaw(map[string]any{"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": `{"code":"text(1)"}`}),
		migrationRaw(map[string]any{"type": "function_call_output", "call_id": "call-1", "output": []any{map[string]any{"type": "input_text", "text": "done"}}}),
		migrationRaw(map[string]any{"type": "reasoning_effort_restore", "input": "high"}),
	}
	converted, err := convertLegacyRecords(protocol.APIStyleAnthropicMessages, records)
	if err != nil {
		t.Fatal(err)
	}
	if converted.pendingEffort != "" {
		t.Fatalf("effort restore after user result should have become a control message: %q", converted.pendingEffort)
	}
	if len(converted.records) != 4 {
		t.Fatalf("converted records=%d, want user, native assistant, tool result, and effort control: %s", len(converted.records), protocol.MustJSON(converted.records))
	}
	if !bytes.Equal(converted.records[1], native) {
		t.Fatalf("native Anthropic message changed: got %s want %s", converted.records[1], native)
	}
	for _, record := range converted.records {
		fields, err := legacyObject(record)
		if err != nil {
			t.Fatal(err)
		}
		if kind := legacyString(fields["type"]); kind == "anthropic_message" || kind == "function_call" || kind == "reasoning_effort_restore" {
			t.Fatalf("legacy wrapper/projection/control remained: %s", record)
		}
	}
	var toolResult map[string]json.RawMessage
	if err := json.Unmarshal(converted.records[2], &toolResult); err != nil {
		t.Fatal(err)
	}
	var content []map[string]json.RawMessage
	if err := json.Unmarshal(toolResult["content"], &content); err != nil {
		t.Fatal(err)
	}
	var toolUseID string
	if len(content) != 1 || json.Unmarshal(content[0]["tool_use_id"], &toolUseID) != nil || toolUseID != "call-1" {
		t.Fatalf("converted tool result=%s", converted.records[2])
	}
	var control map[string]json.RawMessage
	if err := json.Unmarshal(converted.records[3], &control); err != nil {
		t.Fatal(err)
	}
	var outputConfig map[string]string
	if err := json.Unmarshal(control["output_config"], &outputConfig); err != nil || outputConfig["effort"] != "high" {
		t.Fatalf("effort control=%s err=%v", converted.records[3], err)
	}
}

func TestConvertLegacyAnthropicEffortRestoreCarriesPendingState(t *testing.T) {
	compaction := json.RawMessage(`{"id":"msg-compact","role":"assistant","content":[{"type":"compaction","content":"summary","signature":"sig-compact"}],"stop_reason":"compaction"}`)
	retainedUser := json.RawMessage(`{"id":"msg-user","role":"user","content":[{"type":"text","text":"retained suffix"}],"stop_reason":null}`)
	converted, err := convertLegacyRecords(protocol.APIStyleAnthropicMessages, []json.RawMessage{
		anthropicEnvelope(compaction),
		migrationRaw(map[string]any{"type": "reasoning_effort_restore", "input": "xhigh"}),
		anthropicEnvelope(retainedUser),
	})
	if err != nil {
		t.Fatal(err)
	}
	if converted.pendingEffort != "xhigh" {
		t.Fatalf("pending effort=%q", converted.pendingEffort)
	}
	if len(converted.records) != 2 || !bytes.Equal(converted.records[0], compaction) || !bytes.Equal(converted.records[1], retainedUser) {
		t.Fatalf("retained native suffix/signature changed: %s", protocol.MustJSON(converted.records))
	}
	wire, err := nativeContextWire(protocol.APIStyleAnthropicMessages, converted)
	if err != nil {
		t.Fatal(err)
	}
	var state map[string]json.RawMessage
	if err := json.Unmarshal(wire, &state); err != nil {
		t.Fatal(err)
	}
	var pending string
	if err := json.Unmarshal(state["pending_effort"], &pending); err != nil || pending != "xhigh" {
		t.Fatalf("native pending effort=%q state=%s err=%v", pending, wire, err)
	}
	decoded, err := decodeNativeContext(protocol.APIStyleAnthropicMessages, wire)
	if err != nil {
		t.Fatal(err)
	}
	active := decoded.(*anthropic.Context)
	if active.PendingEffort == nil || *active.PendingEffort != "xhigh" {
		t.Fatalf("decoded Anthropic pending effort=%v", active.PendingEffort)
	}
	var messages []json.RawMessage
	if err := json.Unmarshal(state["messages"], &messages); err != nil || len(messages) != 2 {
		t.Fatalf("native messages=%s err=%v", state["messages"], err)
	}
	var retained map[string]json.RawMessage
	if err := json.Unmarshal(messages[0], &retained); err != nil {
		t.Fatal(err)
	}
	var blocks []map[string]json.RawMessage
	if err := json.Unmarshal(retained["content"], &blocks); err != nil {
		t.Fatal(err)
	}
	var signature string
	if len(blocks) != 1 || json.Unmarshal(blocks[0]["signature"], &signature) != nil || signature != "sig-compact" {
		t.Fatalf("compaction signature lost from active context: %s", messages[0])
	}
}

func TestLegacyAnthropicReplayMatchesPriorWireMessages(t *testing.T) {
	assistant := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"thinking","thinking":"signed thought","signature":"sig-thinking"},{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use","usage":{"input_tokens":7}}`)
	compaction := json.RawMessage(`{"id":"msg-compact","role":"assistant","content":[{"type":"compaction","content":"retained summary","signature":"sig-compact"}],"stop_reason":"compaction","usage":{"input_tokens":11}}`)
	records := []json.RawMessage{
		migrationRaw(map[string]any{"type": "message", "role": "user", "content": "run it"}),
		anthropicEnvelope(assistant),
		migrationRaw(map[string]any{"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": `{"code":"text(1)"}`}),
		migrationRaw(map[string]any{"type": "function_call_output", "call_id": "call-1", "output": []any{map[string]any{"type": "input_text", "text": "done"}}}),
		migrationRaw(map[string]any{"type": "reasoning_effort", "input": "high"}),
		anthropicEnvelope(compaction),
		migrationRaw(map[string]any{"type": "reasoning_effort_restore", "input": "xhigh"}),
		migrationRaw(map[string]any{"type": "message", "role": "user", "content": "continue"}),
	}
	converted, err := convertLegacyRecords(protocol.APIStyleAnthropicMessages, records)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := nativeContextWire(protocol.APIStyleAnthropicMessages, converted)
	if err != nil {
		t.Fatal(err)
	}
	state, err := decodeNativeContext(protocol.APIStyleAnthropicMessages, wire)
	if err != nil {
		t.Fatal(err)
	}
	actual, err := state.(*anthropic.Context).WireMessages()
	if err != nil {
		t.Fatal(err)
	}
	expected := []json.RawMessage{
		json.RawMessage(`{"role":"user","content":[{"type":"text","text":"run it"}]}`),
		json.RawMessage(`{"role":"assistant","content":[{"type":"thinking","thinking":"signed thought","signature":"sig-thinking"},{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}]}`),
		json.RawMessage(`{"role":"user","content":[{"type":"tool_result","tool_use_id":"call-1","content":[{"type":"text","text":"done"}]}]}`),
		json.RawMessage(`{"role":"system","content":[],"output_config":{"effort":"high"}}`),
		json.RawMessage(`{"role":"assistant","content":[{"type":"compaction","content":"retained summary","signature":"sig-compact"}]}`),
		json.RawMessage(`{"role":"user","content":[{"type":"text","text":"continue"}]}`),
		json.RawMessage(`{"role":"system","content":[],"output_config":{"effort":"xhigh"}}`),
	}
	assertJSONRecordSequence(t, actual, expected)
}

func assertJSONRecordSequence(t *testing.T, actual, expected []json.RawMessage) {
	t.Helper()
	if len(actual) != len(expected) {
		t.Fatalf("wire record count=%d want=%d: %s", len(actual), len(expected), protocol.MustJSON(actual))
	}
	for i := range actual {
		got, err := canonicalLegacyJSON(actual[i])
		if err != nil {
			t.Fatal(err)
		}
		want, err := canonicalLegacyJSON(expected[i])
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want) {
			t.Errorf("wire record %d differs: got %s want %s", i+1, actual[i], expected[i])
		}
	}
}

func TestConvertLegacyResponsesDropsEffortMarkersAndPreservesNativeJSON(t *testing.T) {
	native := json.RawMessage(`{"type":"message","id":"resp-1","role":"assistant","content":[{"type":"output_text","text":"precise"}],"opaque_number":9007199254740993,"decimal":1.234567890123456789}`)
	converted, err := convertLegacyRecords(protocol.APIStyleResponses, []json.RawMessage{
		native,
		migrationRaw(map[string]any{"type": "reasoning_effort", "input": "high"}),
		migrationRaw(map[string]any{"type": "reasoning_effort_restore", "input": "xhigh"}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(converted.records) != 1 || !bytes.Equal(converted.records[0], native) {
		t.Fatalf("Responses native JSON changed or effort marker remained: %s", protocol.MustJSON(converted.records))
	}
	state, err := decodeNativeRecords(protocol.APIStyleResponses, converted.records)
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := state.Records()
	if err != nil {
		t.Fatal(err)
	}
	if len(replayed) != 1 || !bytes.Equal(replayed[0], native) {
		t.Fatalf("decoded Responses item did not preserve provider JSON and numeric precision: %s", protocol.MustJSON(replayed))
	}
}

func TestConvertLegacyAnthropicRejectsMismatchedProjection(t *testing.T) {
	native := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use"}`)
	_, err := convertLegacyRecords(protocol.APIStyleAnthropicMessages, []json.RawMessage{
		anthropicEnvelope(native),
		migrationRaw(map[string]any{"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": `{"code":"text(2)"}`}),
	})
	if err == nil || !strings.Contains(err.Error(), "does not match native tool_use") {
		t.Fatalf("mismatched projection error=%v", err)
	}
}

func TestConvertLegacyAnthropicRejectsMissingProjection(t *testing.T) {
	native := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use"}`)
	_, err := convertLegacyRecords(protocol.APIStyleAnthropicMessages, []json.RawMessage{anthropicEnvelope(native)})
	if err == nil || !strings.Contains(err.Error(), "0 of 1 matching function_call projections") {
		t.Fatalf("missing projection error=%v", err)
	}
}

func TestNativeMigrationUsesIntentAPIAndPreservesSessionEvidenceRepeatedly(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	nativeAssistant := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"thinking","thinking":"keep","signature":"sig-keep"},{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use","usage":{"input_tokens":7}}`)
	nativeCompaction := json.RawMessage(`{"id":"msg-compact","role":"assistant","content":[{"type":"compaction","content":"summary","signature":"sig-compact"}],"stop_reason":"compaction"}`)
	nativeSuffix := json.RawMessage(`{"id":"msg-suffix","role":"user","content":[{"type":"text","text":"retained suffix"}],"stop_reason":null}`)
	archive := []json.RawMessage{
		migrationRaw(map[string]any{"type": "message", "role": "user", "content": "run it"}),
		anthropicEnvelope(nativeAssistant),
		migrationRaw(map[string]any{"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": `{"code":"text(1)"}`}),
		migrationRaw(map[string]any{"type": "function_call_output", "call_id": "call-1", "output": []any{map[string]any{"type": "input_text", "text": "done"}}}),
	}
	active := []json.RawMessage{
		anthropicEnvelope(nativeCompaction),
		migrationRaw(map[string]any{"type": "reasoning_effort_restore", "input": "xhigh"}),
		anthropicEnvelope(nativeSuffix),
	}
	installLegacyConversation(t, store, session.ConversationID, archive, active)
	intentID, _ := addMigrationIntent(t, store, session.ConversationID, string(protocol.APIStyleAnthropicMessages))
	unresolvedID := uuid.NewString()
	unresolvedRequest := migrationRaw(map[string]any{"kind": "sampling", "api": string(protocol.APIStyleAnthropicMessages), "opaque": "unresolved effect"})
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,0,$3::jsonb,NULL)`, unresolvedID, session.ConversationID, []byte(unresolvedRequest)); err != nil {
		t.Fatal(err)
	}
	unknownPending := []byte(`{"future_effect":{"token":"keep-me","sequence":9007199254740993}}`)
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET pending=$2::jsonb WHERE id=$1`, session.ConversationID, unknownPending); err != nil {
		t.Fatal(err)
	}
	var beforePending []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT pending FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&beforePending); err != nil {
		t.Fatal(err)
	}
	var beforeUnresolvedRequest string
	var beforeUnresolvedResult sql.NullString
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request::text,result::text FROM sampler_intent WHERE id=$1`, unresolvedID).Scan(&beforeUnresolvedRequest, &beforeUnresolvedResult); err != nil {
		t.Fatal(err)
	}
	var beforeRequest, beforeResult string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request::text,result::text FROM sampler_intent WHERE id=$1`, intentID).Scan(&beforeRequest, &beforeResult); err != nil {
		t.Fatal(err)
	}
	var sequence int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT COALESCE(MAX(sequence),0)+1 FROM sampler_event WHERE session_id=$1`, session.ID).Scan(&sequence); err != nil {
		t.Fatal(err)
	}
	eventWire := []byte("unchanged event bytes")
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_event(session_id,sequence,entry) VALUES($1,$2,$3)`, session.ID, sequence, eventWire); err != nil {
		t.Fatal(err)
	}

	// The configured route says Responses, but durable provider request evidence consistently says
	// Anthropic. Requests are authoritative once present.
	modelAPIs := map[string]protocol.APIStyle{"fixture-model": protocol.APIStyleResponses}
	if err := Migrate(t.Context(), store.DB(), modelAPIs); err != nil {
		t.Fatal(err)
	}
	if err := Migrate(t.Context(), store.DB(), modelAPIs); err != nil {
		t.Fatalf("repeated startup migration: %v", err)
	}
	var api string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT api FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&api); err != nil || api != string(protocol.APIStyleAnthropicMessages) {
		t.Fatalf("conversation API=%q err=%v", api, err)
	}
	var sessionID, conversationID string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT id,conversation_id::text FROM sampler_session WHERE id=$1`, session.ID).Scan(&sessionID, &conversationID); err != nil || sessionID != session.ID || conversationID != session.ConversationID {
		t.Fatalf("session identity changed: id=%q conversation=%q err=%v", sessionID, conversationID, err)
	}
	var afterRequest, afterResult string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request::text,result::text FROM sampler_intent WHERE id=$1`, intentID).Scan(&afterRequest, &afterResult); err != nil {
		t.Fatal(err)
	}
	if beforeRequest != afterRequest || beforeResult != afterResult {
		t.Fatalf("intent evidence changed: before=%s/%s after=%s/%s", beforeRequest, beforeResult, afterRequest, afterResult)
	}
	var afterPending []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT pending FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&afterPending); err != nil {
		t.Fatal(err)
	}
	var afterUnresolvedRequest string
	var afterUnresolvedResult sql.NullString
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request::text,result::text FROM sampler_intent WHERE id=$1`, unresolvedID).Scan(&afterUnresolvedRequest, &afterUnresolvedResult); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(beforePending, afterPending) || beforeUnresolvedRequest != afterUnresolvedRequest || beforeUnresolvedResult != afterUnresolvedResult || afterUnresolvedResult.Valid {
		t.Fatalf("pending or unresolved effect changed: pending %s -> %s; intent %s/%v -> %s/%v", beforePending, afterPending, beforeUnresolvedRequest, beforeUnresolvedResult, afterUnresolvedRequest, afterUnresolvedResult)
	}
	var afterEvent []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, session.ID, sequence).Scan(&afterEvent); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(afterEvent, eventWire) {
		t.Fatalf("event bytes changed: %q", afterEvent)
	}

	archiveAfter := legacyRecordSnapshot(t, store, session.ConversationID)
	if len(archiveAfter) != 3 {
		t.Fatalf("archive records=%d; expected native user, assistant, and tool-result messages: %v", len(archiveAfter), archiveAfter)
	}
	for _, wire := range archiveAfter {
		var record map[string]json.RawMessage
		if err := json.Unmarshal([]byte(wire), &record); err != nil {
			t.Fatal(err)
		}
		if kind := legacyString(record["type"]); kind == "anthropic_message" || kind == "function_call" || kind == "function_call_output" {
			t.Fatalf("legacy record survived conversion: %s", wire)
		}
	}
	var contextWire []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT context FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&contextWire); err != nil {
		t.Fatal(err)
	}
	var activeState map[string]json.RawMessage
	if err := json.Unmarshal(contextWire, &activeState); err != nil {
		t.Fatal(err)
	}
	var pending string
	if err := json.Unmarshal(activeState["pending_effort"], &pending); err != nil || pending != "xhigh" {
		t.Fatalf("restored effort state=%q context=%s err=%v", pending, contextWire, err)
	}
	var messages []json.RawMessage
	if err := json.Unmarshal(activeState["messages"], &messages); err != nil || len(messages) != 2 {
		t.Fatalf("active retained context=%s err=%v", contextWire, err)
	}
}

func TestNativeMigrationUsesConfiguredModelAPIWithoutRequestEvidence(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	archive := []json.RawMessage{migrationRaw(map[string]any{"type": "message", "role": "user", "content": "hello"})}
	installLegacyConversation(t, store, session.ConversationID, archive, nil)
	addLegacyIntentWithoutAPI(t, store, session.ConversationID)
	if err := Migrate(t.Context(), store.DB(), map[string]protocol.APIStyle{"fixture-model": protocol.APIStyleAnthropicMessages}); err != nil {
		t.Fatal(err)
	}
	var api string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT api FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&api); err != nil || api != string(protocol.APIStyleAnthropicMessages) {
		t.Fatalf("configured API=%q err=%v", api, err)
	}
}

func TestNativeMigrationRecognizesLegacyStandaloneConversation(t *testing.T) {
	store := newTestStore(t)
	conversationID := uuid.NewString()
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_conversation ALTER COLUMN api DROP NOT NULL`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_conversation(id,api,generation,context) VALUES($1,NULL,0,'[]'::jsonb)`, conversationID); err != nil {
		t.Fatal(err)
	}
	archive := []json.RawMessage{migrationRaw(map[string]any{"type": "message", "role": "user", "content": "standalone"})}
	installLegacyConversation(t, store, conversationID, archive, nil)
	addLegacyIntentWithoutAPI(t, store, conversationID)
	if err := Migrate(t.Context(), store.DB(), nil); err != nil {
		t.Fatal(err)
	}
	var api string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT api FROM sampler_conversation WHERE id=$1`, conversationID).Scan(&api); err != nil || api != string(protocol.APIStyleResponses) {
		t.Fatalf("standalone API=%q err=%v", api, err)
	}
}

func TestNativeMigrationRejectsAmbiguousAndLiveConversationsBeforeChangingArchive(t *testing.T) {
	t.Run("missing model mapping", func(t *testing.T) {
		store := newTestStore(t)
		session := openSessionForStore(t, store, "")
		archive := []json.RawMessage{migrationRaw(map[string]any{"type": "message", "role": "user", "content": "preserve me"})}
		installLegacyConversation(t, store, session.ConversationID, archive, nil)
		addLegacyIntentWithoutAPI(t, store, session.ConversationID)
		before := legacyRecordSnapshot(t, store, session.ConversationID)
		err := Migrate(t.Context(), store.DB(), nil)
		if err == nil || !strings.Contains(err.Error(), "no explicit configured API mapping") {
			t.Fatalf("ambiguous migration error=%v", err)
		}
		after := legacyRecordSnapshot(t, store, session.ConversationID)
		if len(before) != len(after) || before[0] != after[0] {
			t.Fatalf("failed conversion modified archive: before=%v after=%v", before, after)
		}
		var api sql.NullString
		var active []byte
		if err := store.DB().QueryRowContext(t.Context(), `SELECT api,context FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&api, &active); err != nil {
			t.Fatal(err)
		}
		if api.Valid || active != nil {
			t.Fatalf("failed conversion modified conversation state: api=%v context=%s", api, active)
		}
	})

	t.Run("conflicting request APIs reject conversion", func(t *testing.T) {
		store := newTestStore(t)
		session := openSessionForStore(t, store, "")
		archive := []json.RawMessage{migrationRaw(map[string]any{"type": "message", "role": "user", "content": "preserve me"})}
		installLegacyConversation(t, store, session.ConversationID, archive, nil)
		addMigrationIntent(t, store, session.ConversationID, string(protocol.APIStyleResponses))
		addMigrationIntent(t, store, session.ConversationID, string(protocol.APIStyleAnthropicMessages))
		before := legacyRecordSnapshot(t, store, session.ConversationID)
		err := Migrate(t.Context(), store.DB(), map[string]protocol.APIStyle{"fixture-model": protocol.APIStyleResponses})
		if err == nil || !strings.Contains(err.Error(), "conflicting intent API evidence") {
			t.Fatalf("conflicting intent migration error=%v", err)
		}
		after := legacyRecordSnapshot(t, store, session.ConversationID)
		if len(before) != len(after) || before[0] != after[0] {
			t.Fatalf("conflicting intents modified archive: before=%v after=%v", before, after)
		}
	})

	t.Run("live lease", func(t *testing.T) {
		store := newTestStore(t)
		session := openSessionForStore(t, store, "")
		if _, err := store.Claim(t.Context(), session.ConversationID, time.Minute); err != nil {
			t.Fatal(err)
		}
		archive := []json.RawMessage{migrationRaw(map[string]any{"type": "message", "role": "user", "content": "live state"})}
		installLegacyConversation(t, store, session.ConversationID, archive, nil)
		before := legacyRecordSnapshot(t, store, session.ConversationID)
		err := Migrate(t.Context(), store.DB(), map[string]protocol.APIStyle{"fixture-model": protocol.APIStyleResponses})
		if err == nil || !strings.Contains(err.Error(), "has a live lease") {
			t.Fatalf("live lease migration error=%v", err)
		}
		after := legacyRecordSnapshot(t, store, session.ConversationID)
		if len(before) != len(after) || before[0] != after[0] {
			t.Fatalf("live lease check modified archive: before=%v after=%v", before, after)
		}
	})
}

func TestNativeMigrationRejectsProjectionMismatchAndRollsBackAllWrites(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	native := json.RawMessage(`{"id":"msg-assistant","role":"assistant","content":[{"type":"tool_use","id":"call-1","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use"}`)
	archive := []json.RawMessage{
		anthropicEnvelope(native),
		migrationRaw(map[string]any{"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": `{"code":"text(2)"}`}),
	}
	installLegacyConversation(t, store, session.ConversationID, archive, archive)
	addMigrationIntent(t, store, session.ConversationID, string(protocol.APIStyleAnthropicMessages))
	before := legacyRecordSnapshot(t, store, session.ConversationID)
	var beforeContext []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT context FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&beforeContext); err != nil {
		t.Fatal(err)
	}
	err := Migrate(t.Context(), store.DB(), map[string]protocol.APIStyle{"fixture-model": protocol.APIStyleResponses})
	if err == nil || !strings.Contains(err.Error(), "does not match native tool_use") {
		t.Fatalf("projection mismatch migration error=%v", err)
	}
	after := legacyRecordSnapshot(t, store, session.ConversationID)
	var afterContext []byte
	var api sql.NullString
	if err := store.DB().QueryRowContext(t.Context(), `SELECT api,context FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&api, &afterContext); err != nil {
		t.Fatal(err)
	}
	if len(before) != len(after) || before[0] != after[0] || before[1] != after[1] || !bytes.Equal(beforeContext, afterContext) || api.Valid {
		t.Fatalf("rejected conversion changed legacy state: before=%v/%s, after=%v/%s api=%v", before, beforeContext, after, afterContext, api)
	}
}
