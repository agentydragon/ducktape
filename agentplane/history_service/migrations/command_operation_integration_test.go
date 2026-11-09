package migrations

import (
	"database/sql"
	"testing"
)

func insertCommand(t *testing.T, db *sql.DB, id, state string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,state,entity_index)
        VALUES('00000000-0000-4000-8000-000000000012','epoch','command',$1,1,1,false,$2,(SELECT count(*) FROM thread_entity))`, id, state); err != nil {
		t.Fatal(err)
	}
}

func storedOperation(t *testing.T, db *sql.DB, id string) string {
	t.Helper()
	var operation string
	if err := db.QueryRowContext(t.Context(), `SELECT state->'operation' FROM thread_entity WHERE entity_id=$1`, id).Scan(&operation); err != nil {
		t.Fatal(err)
	}
	return operation
}

// Command rows a pre-enum fold wrote, before or during the rollout, spell the enum value name.
func TestCommandOperationsBecomeEnumNames(t *testing.T) {
	db := baselineDB(t)
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 11); err != nil {
		t.Fatal(err)
	}
	insertCommand(t, db, "stored-input", `{"operation":"submit_input","outcome":"THREAD_COMMAND_OUTCOME_PENDING"}`)
	insertCommand(t, db, "stored-stop", `{"operation":"stop_runner_session","outcome":"THREAD_COMMAND_OUTCOME_EFFECTED"}`)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	insertCommand(t, db, "overlapping-compact", `{"operation":"compact","outcome":"THREAD_COMMAND_OUTCOME_PENDING"}`)
	insertCommand(t, db, "current-model", `{"operation":"OPERATION_CHANGE_MODEL","outcome":"THREAD_COMMAND_OUTCOME_PENDING"}`)
	for id, want := range map[string]string{
		"stored-input":        `"OPERATION_SUBMIT_INPUT"`,
		"stored-stop":         `4`,
		"overlapping-compact": `"OPERATION_COMPACT"`,
		"current-model":       `"OPERATION_CHANGE_MODEL"`,
	} {
		if got := storedOperation(t, db, id); got != want {
			t.Errorf("%s operation = %s, want %s", id, got, want)
		}
	}
}
