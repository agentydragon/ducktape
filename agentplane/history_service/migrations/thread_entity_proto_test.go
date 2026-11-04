package migrations

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func migrationDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 6); err != nil {
		t.Fatal(err)
	}
	return db
}

func insertLegacy(t *testing.T, db *sql.DB, row threadEntityV6, index int64) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,turn_id,state,text_ref,arguments_ref,output_ref,input_ref,entity_index) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`, row.ThreadID, row.ProjectionEpoch, row.EntityKind, row.EntityID, row.Cursor, row.RevisionCursor, row.Pending, row.TurnID, string(row.State), nullableJSON(row.TextRef), nullableJSON(row.ArgumentsRef), nullableJSON(row.OutputRef), nullableJSON(row.InputRef), index); err != nil {
		t.Fatal(err)
	}
}

func nullableJSON(value []byte) any {
	if len(value) == 0 {
		return nil
	}
	return string(value)
}

func legacyRow(kind, id, state string) threadEntityV6 {
	return threadEntityV6{ThreadID: "00000000-0000-0000-0000-000000000001", ProjectionEpoch: "old-python", EntityKind: kind, EntityID: id, Cursor: 9007199254740993, RevisionCursor: math.MaxInt64, State: json.RawMessage(state)}
}

func TestThreadEntityMigrationPreservesAllKindsAndHistory(t *testing.T) {
	db := migrationDB(t)
	turn := "" // Presence of an empty optional string must survive.
	ref := `{"projection_epoch":"old-python","owner_cursor":"9007199254740993","owner_id":"tool","field":"output","revision_cursor":"9223372036854775807","generation":"9007199254740993","chunk_count":"18446744073709551615"}`
	item := legacyRow("item", "tool", `{"kind":3,"tool_name":"Bash","completion":"tool","tool_succeeded":false,"recovery":0,"recovery_reason":""}`)
	item.TurnID = &turn
	item.OutputRef = json.RawMessage(ref)
	input := legacyRow("confirmed_input", "confirmed", `{"harness_message_id":"message","origin_command_ids":["first","second"]}`)
	input.TurnID = proto.String("turn")
	input.InputRef = json.RawMessage(`{"projection_epoch":"old-python","owner_cursor":"9007199254740993","owner_id":"message","field":"confirmed_input","revision_cursor":"9007199254740993","generation":"9007199254740993","chunk_count":"0"}`)
	rows := []threadEntityV6{
		legacyRow("view_state", "current", `{"controls":{"applied_model":"","active_turn_id":null,"harness_state":"stopped"},"operational":{"status":"failed","last_verified_cursor":"9223372036854775807","feed_error":{"cursor":"0","message":"failure"}}}`),
		item, input,
		legacyRow("lifecycle", "lifecycle", `{"observation":"setup_output","event":{"setupOutput":{"stderr":"AAEC"}}}`),
		legacyRow("command", "command", `{"operation":"change_model","outcome":"failed","outcome_cursor":"0","outcome_reason":"","requested_value":""}`),
	}
	for n, row := range rows {
		insertLegacy(t, db, row, int64(n))
	}
	// More than one keyset batch, spanning identities, without buffering or rewriting raw history.
	for n := 0; n < threadEntityBatchSize+3; n++ {
		insertLegacy(t, db, legacyRow("item", fmt.Sprintf("text-%03d", n), `{"kind":1,"tool_name":"","completion":"text","tool_succeeded":null,"recovery":null,"recovery_reason":null}`), int64(n+len(rows)))
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,0);`, item.ThreadID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,0)`, item.ThreadID, []byte{0, 1, 2, 255}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_payload_chunk(thread_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text) VALUES($1,'old-python',1,'tool','output',1,0,$2::json)`, item.ThreadID, `"before\u0000after"`); err != nil {
		t.Fatal(err)
	}
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	var stored []byte
	read := func(kind, id string, into proto.Message) {
		t.Helper()
		if err := db.QueryRowContext(t.Context(), `SELECT state FROM thread_entity WHERE entity_kind=$1 AND entity_id=$2`, kind, id).Scan(&stored); err != nil {
			t.Fatal(err)
		}
		if err := protojson.Unmarshal(stored, into); err != nil {
			t.Fatal(err)
		}
	}
	var tool historypb.ThreadItem
	read("item", "tool", &tool)
	if tool.GetTool() == nil || tool.GetTool().GetSucceeded() || tool.TurnId == nil || *tool.TurnId != "" || tool.Recovery == nil || *tool.Recovery != 0 || tool.RecoveryReason == nil || *tool.RecoveryReason != "" {
		t.Fatalf("lost tool completion/optional presence: %v", &tool)
	}
	if tool.OutputRef.GetOwnerCursor() != 9007199254740993 || tool.OutputRef.GetRevisionCursor() != math.MaxInt64 || tool.OutputRef.GetChunkCount() != math.MaxUint64 {
		t.Fatalf("lost exact uint64 reference: %v", tool.OutputRef)
	}
	var confirmed historypb.ThreadConfirmedInput
	read("confirmed_input", "confirmed", &confirmed)
	if confirmed.GetTurnId() != "turn" || len(confirmed.OriginCommandIds) != 2 || confirmed.InputRef.GetField() != historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_CONFIRMED_INPUT {
		t.Fatalf("lost input: %v", &confirmed)
	}
	var lifecycle historypb.ThreadLifecycle
	read("lifecycle", "lifecycle", &lifecycle)
	if string(lifecycle.GetEvent().GetSetupOutput().GetStderr()) != "\x00\x01\x02" {
		t.Fatalf("lost native bytes: %v", &lifecycle)
	}
	var command historypb.ThreadCommand
	read("command", "command", &command)
	if command.OutcomeCursor == nil || *command.OutcomeCursor != 0 || command.OutcomeReason == nil || command.RequestedValue == nil {
		t.Fatalf("lost optional command fields: %v", &command)
	}
	var view historypb.ThreadViewState
	read("view_state", "current", &view)
	if view.Controls.AppliedModel == nil || view.Controls.ActiveTurnId != nil || view.Controls.AppliedReasoningEffort != nil || view.Operational.LastVerifiedCursor != math.MaxInt64 || view.Operational.FeedError.Cursor == nil {
		t.Fatalf("lost optional view state: %v", &view)
	}
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM information_schema.columns WHERE table_name='thread_entity' AND column_name IN ('turn_id','text_ref','arguments_ref','output_ref','input_ref')`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("obsolete columns: %d, %v", count, err)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM thread_entity WHERE cursor=9007199254740993 AND revision_cursor=9223372036854775807`).Scan(&count); err != nil || count != len(rows)+threadEntityBatchSize+3 {
		t.Fatalf("lost indexed rows: %d, %v", count, err)
	}
	var raw []byte
	if err := db.QueryRowContext(t.Context(), `SELECT payload FROM session_event`).Scan(&raw); err != nil || string(raw) != string([]byte{0, 1, 2, 255}) {
		t.Fatalf("raw history changed: %x, %v", raw, err)
	}
	var body string
	if err := db.QueryRowContext(t.Context(), `SELECT text::text FROM thread_payload_chunk`).Scan(&body); err != nil || body != `"before\u0000after"` {
		t.Fatalf("payload history changed: %q, %v", body, err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatalf("repeat startup: %v", err)
	}
}

func TestThreadEntityMigrationFailureRollsBackAndRestarts(t *testing.T) {
	db := migrationDB(t)
	good := legacyRow("command", "a", `{"operation":"interrupt","outcome":"pending","outcome_cursor":null,"outcome_reason":null}`)
	good.Pending = true
	insertLegacy(t, db, good, 0)
	// The bad row comes after a full batch, so the first bulk update must also roll back.
	for n := 0; n < threadEntityBatchSize; n++ {
		row := legacyRow("command", fmt.Sprintf("b-%03d", n), `{"operation":"interrupt","outcome":"pending","outcome_cursor":null,"outcome_reason":null}`)
		row.Pending = true
		insertLegacy(t, db, row, int64(n+1))
	}
	bad := legacyRow("command", "z", `{"operation":"interrupt","outcome":"unknown","outcome_cursor":null,"outcome_reason":null}`)
	insertLegacy(t, db, bad, threadEntityBatchSize+1)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(t.Context()); err == nil {
		t.Fatal("malformed row accepted")
	}
	var state []byte
	if err := db.QueryRowContext(t.Context(), `SELECT state FROM thread_entity WHERE entity_id='a'`).Scan(&state); err != nil {
		t.Fatal(err)
	}
	var original map[string]any
	if err := json.Unmarshal(state, &original); err != nil {
		t.Fatal(err)
	}
	if original["outcome"] != "pending" {
		t.Fatalf("partial state conversion committed: %s", state)
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE thread_entity SET state=$1::jsonb WHERE entity_id='z'`, string(good.State)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE thread_entity SET pending=true WHERE entity_id='z'`); err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatalf("restart after repair: %v", err)
	}
}

func TestCanonicalThreadStateFailsClosed(t *testing.T) {
	base := legacyRow("item", "item", `{"kind":1,"tool_name":"","completion":null,"tool_succeeded":null,"recovery":null,"recovery_reason":null}`)
	for _, test := range []struct {
		name   string
		change func(*threadEntityV6)
	}{
		{"unknown kind", func(r *threadEntityV6) { r.EntityKind = "future" }},
		{"missing field", func(r *threadEntityV6) { r.State = []byte(`{"kind":1}`) }},
		{"null required", func(r *threadEntityV6) {
			r.State = []byte(`{"kind":null,"tool_name":"","completion":null,"tool_succeeded":null,"recovery":null,"recovery_reason":null}`)
		}},
		{"unknown state field", func(r *threadEntityV6) {
			r.State = []byte(`{"kind":1,"tool_name":"","completion":null,"tool_succeeded":null,"recovery":null,"recovery_reason":null,"extra":"would be lost"}`)
		}},
		{"inconsistent completion", func(r *threadEntityV6) {
			r.State = []byte(`{"kind":1,"tool_name":"","completion":"tool","tool_succeeded":null,"recovery":null,"recovery_reason":null}`)
		}},
		{"unexpected column", func(r *threadEntityV6) { r.EntityKind = "lifecycle"; r.TextRef = []byte(`{}`) }},
		{"negative uint64", func(r *threadEntityV6) {
			r.TextRef = []byte(`{"projection_epoch":"old-python","owner_cursor":"-1","owner_id":"item","field":"text","revision_cursor":"1","generation":"1","chunk_count":"1"}`)
		}},
		{"overflow uint64", func(r *threadEntityV6) {
			r.TextRef = []byte(`{"projection_epoch":"old-python","owner_cursor":"18446744073709551616","owner_id":"item","field":"text","revision_cursor":"1","generation":"1","chunk_count":"1"}`)
		}},
		{"lifecycle discriminator", func(r *threadEntityV6) {
			r.EntityKind = "lifecycle"
			r.State = []byte(`{"observation":"harness_lost","event":{"harnessStarted":{}}}`)
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			row := base
			test.change(&row)
			if _, err := row.canonicalState(); err == nil {
				t.Fatal("malformed row accepted")
			}
		})
	}
}
