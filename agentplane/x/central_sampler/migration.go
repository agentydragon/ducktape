package centralsampler

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed schema/*.sql
var migrationFiles embed.FS

// Migrate applies the sampler-owned Goose SQL migrations.
func Migrate(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(migrationFiles, "schema")
	if err != nil {
		return err
	}
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(7241821))
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("central_sampler_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return fmt.Errorf("configure central sampler migrations: %w", err)
	}
	if _, err := provider.Up(ctx); err != nil {
		return err
	}
	return verifySchema(ctx, db)
}

func verifySchema(ctx context.Context, db *sql.DB) error {
	expected := map[string]map[string]string{
		"sampler_conversation":      {"id": "uuid", "generation": "integer", "owner": "uuid", "lease_until": "timestamp with time zone", "pending": "jsonb", "stopped": "boolean", "executor_session": "character varying", "context_items": "jsonb", "instructions": "text", "context_tokens": "integer"},
		"sampler_conversation_item": {"conversation_id": "uuid", "ordinal": "bigint", "item": "jsonb"},
		"sampler_intent":            {"id": "uuid", "conversation_id": "uuid", "generation": "integer", "request": "jsonb", "result": "jsonb"},
		"sampler_process":           {"id": "character varying", "conversation_id": "uuid", "generation": "integer", "after_seq": "integer", "status": "character varying", "tty": "boolean", "exit_code": "integer", "buffered_output": "bytea"},
		"sampler_cell":              {"id": "character varying", "conversation_id": "uuid", "generation": "integer", "status": "character varying"},
		"sampler_interrupt":         {"id": "uuid", "conversation_id": "uuid", "generation": "integer", "target_turn": "character varying", "result": "jsonb"},
		"sampler_session":           {"id": "character varying", "conversation_id": "uuid", "account_uid": "character varying", "configuration": "bytea", "model": "character varying", "effort": "character varying", "source_id": "character varying", "cursor": "integer", "active_turn": "character varying", "active_command": "character varying", "workspace_prepared": "boolean", "setup_state": "integer", "setup_started": "boolean", "blocked": "boolean", "blocked_reason": "character varying"},
		"sampler_command":           {"session_id": "character varying", "id": "character varying", "payload": "bytea", "admission": "integer", "outcome": "integer", "failure_reason": "character varying"},
		"sampler_event":             {"session_id": "character varying", "sequence": "integer", "entry": "bytea"},
		"sampler_export_checkpoint": {"session_id": "character varying", "destination": "character varying", "cursor": "integer"},
		"sampler_notification":      {"conversation_id": "uuid", "sequence": "bigint", "call_id": "character varying", "cell_id": "character varying", "text": "text"},
	}
	rows, err := db.QueryContext(ctx, `SELECT table_name,column_name,data_type FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name IN (
		'sampler_conversation','sampler_conversation_item','sampler_intent','sampler_process','sampler_cell','sampler_interrupt',
		'sampler_session','sampler_command','sampler_event','sampler_export_checkpoint','sampler_notification')`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var table, column, dataType string
		if err := rows.Scan(&table, &column, &dataType); err != nil {
			return err
		}
		if expectedType, required := expected[table][column]; required && expectedType != dataType {
			return fmt.Errorf("central sampler schema %s.%s has type %s, expected %s", table, column, dataType, expectedType)
		}
		delete(expected[table], column)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for table, columns := range expected {
		for column := range columns {
			return fmt.Errorf("central sampler schema is missing %s.%s", table, column)
		}
	}
	return nil
}
