package journal

import (
	"bytes"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"path/filepath"
	"reflect"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/proto"
)

//go:embed testdata/legacy_version3/*.sql
var legacyJournalMigrations embed.FS

func TestJournalBaselineMatchesHistoricalVersionThreeAndAdoptsEvidence(t *testing.T) {
	historicalPath := filepath.Join(t.TempDir(), "historical.sqlite")
	historical := openJournalFile(t, historicalPath)
	files, err := fs.Sub(legacyJournalMigrations, "testdata/legacy_version3")
	if err != nil {
		t.Fatal(err)
	}
	provider, err := goose.NewProvider(goose.DialectSQLite3, historical, files,
		goose.WithTableName(journalMigrationTableName), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 3); err != nil {
		t.Fatal("create historical version-3 journal:", err)
	}

	freshPath := filepath.Join(t.TempDir(), "fresh.sqlite")
	fresh, err := Open(t.Context(), freshPath, "test-source")
	if err != nil {
		t.Fatal(err)
	}
	if err := fresh.Close(); err != nil {
		t.Fatal(err)
	}
	freshDB := openJournalFile(t, freshPath)
	historicalSchema := journalSchemaSnapshot(t, historical)
	freshSchema := journalSchemaSnapshot(t, freshDB)
	if !reflect.DeepEqual(historicalSchema, freshSchema) {
		t.Fatalf("baseline schema differs from historical version 3:\nold: %v\nnew: %v", historicalSchema, freshSchema)
	}

	command := &commandpb.Command{CommandId: "historical-command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "retained input"}}}
	commandBytes, err := proto.Marshal(command)
	if err != nil {
		t.Fatal(err)
	}
	entry := &eventlogpb.EventEntry{
		Cursor: 1,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-source", Sequence: 1},
		Event:  &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{Command: command}}},
	}
	eventBytes, err := proto.Marshal(entry)
	if err != nil {
		t.Fatal(err)
	}
	statements := []struct {
		query string
		args  []any
	}{
		{"INSERT INTO event_entry(cursor,payload) VALUES(1,?)", []any{eventBytes}},
		{"INSERT INTO command(command_id,payload,admitted_cursor,dispatch_planned,terminal_cursor) VALUES('historical-command',?,1,1,NULL)", []any{commandBytes}},
		{"INSERT INTO native_request(request_id,kind) VALUES('historical-request','claude_submit')", nil},
		{"INSERT INTO native_request_command(request_id,command_id) VALUES('historical-request','historical-command')", nil},
		{"INSERT INTO checkpoint(id,source_id,through_cursor,harness_running,active_turn_id,model,reasoning_effort) VALUES(1,'test-source',1,0,'','test-model','high')", nil},
	}
	for _, statement := range statements {
		if _, err := historical.ExecContext(t.Context(), statement.query, statement.args...); err != nil {
			t.Fatal(err)
		}
	}
	if err := historical.Close(); err != nil {
		t.Fatal(err)
	}

	adopted, err := Open(t.Context(), historicalPath, "test-source")
	if err != nil {
		t.Fatal("adopt historical version-3 journal:", err)
	}
	defer adopted.Close()
	var retainedEvent, retainedCommand []byte
	if err := adopted.conn.QueryRowContext(t.Context(), "SELECT payload FROM event_entry WHERE cursor=1").Scan(&retainedEvent); err != nil {
		t.Fatal(err)
	}
	if err := adopted.conn.QueryRowContext(t.Context(), "SELECT payload FROM command WHERE command_id='historical-command'").Scan(&retainedCommand); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(retainedEvent, eventBytes) || !bytes.Equal(retainedCommand, commandBytes) {
		t.Fatal("adoption rewrote historical EventEntry or command bytes")
	}
	state := adopted.RecoveryState()
	if state.ThroughCursor != 1 || state.Model == nil || *state.Model != "test-model" || state.ReasoningEffort == nil || *state.ReasoningEffort != "high" {
		t.Fatalf("checkpoint changed during adoption: %+v", state)
	}
	requests, err := adopted.PendingNativeRequests(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(requests) != 1 || requests[0].ID != "historical-request" || len(requests[0].Commands) != 1 || requests[0].Commands[0].Command.GetCommandId() != "historical-command" {
		t.Fatalf("native request changed during adoption: %+v", requests)
	}
}

func journalSchemaSnapshot(t *testing.T, db *sql.DB) map[string][]string {
	t.Helper()
	snapshot := make(map[string][]string)
	snapshot["objects"] = readSchemaRows(t, db, `SELECT type || ':' || name || ':' || tbl_name FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' AND name <> 'runner_goose_version'
        ORDER BY type,name`)
	for _, table := range []string{
		"event_entry", "command", "turn_boundary", "checkpoint", "seal", "debug_checkpoint", "native_request", "native_request_command",
	} {
		snapshot[table+":columns"] = readSchemaRows(t, db, `SELECT cid || ':' || name || ':' || type || ':' || "notnull" || ':' || coalesce(dflt_value,'NULL') || ':' || pk
            FROM pragma_table_info(?) ORDER BY cid`, table)
		snapshot[table+":foreign_keys"] = readSchemaRows(t, db, `SELECT id || ':' || seq || ':' || "table" || ':' || "from" || ':' || "to" || ':' || on_update || ':' || on_delete || ':' || "match"
            FROM pragma_foreign_key_list(?) ORDER BY id,seq`, table)
		indexes := readSchemaRows(t, db, `SELECT name FROM pragma_index_list(?) ORDER BY name`, table)
		snapshot[table+":indexes"] = indexes
		for _, index := range indexes {
			snapshot[table+":index:"+index] = readSchemaRows(t, db, `SELECT seqno || ':' || cid || ':' || coalesce(name,'NULL') || ':' || "desc" || ':' || coalesce(coll,'NULL') || ':' || "key"
                FROM pragma_index_xinfo(?) ORDER BY seqno`, index)
		}
	}
	return snapshot
}

func readSchemaRows(t *testing.T, db *sql.DB, query string, args ...any) []string {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), query, args...)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var result []string
	for rows.Next() {
		var item string
		if err := rows.Scan(&item); err != nil {
			t.Fatal(fmt.Errorf("scan schema %q: %w", query, err))
		}
		result = append(result, item)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}
