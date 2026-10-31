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

// Migrate applies the sampler-owned Goose SQL migrations and verifies the native schema.
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
		"sampler_conversation":      {"api": "text", "code_session_epoch": "integer", "code_host_instance": "uuid", "id": "uuid", "generation": "integer", "owner": "uuid", "lease_until": "timestamp with time zone", "pending": "jsonb", "stopped": "boolean", "executor_session": "character varying", "context": "jsonb", "instructions": "text", "context_tokens": "integer", "executor_shell": "character varying", "environment_context": "jsonb"},
		"sampler_conversation_item": {"conversation_id": "uuid", "ordinal": "bigint", "record": "jsonb"},
		"sampler_intent":            {"id": "uuid", "conversation_id": "uuid", "generation": "integer", "request": "jsonb", "result": "jsonb"},
		"sampler_process":           {"id": "character varying", "conversation_id": "uuid", "generation": "integer", "after_seq": "integer", "status": "character varying", "tty": "boolean", "exit_code": "integer", "buffered_output": "bytea", "model_session_id": "integer", "omitted_bytes": "bigint"},
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
	var apiNullable string
	if err := db.QueryRowContext(ctx, `SELECT is_nullable FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name='sampler_conversation' AND column_name='api'`).Scan(&apiNullable); err != nil {
		return fmt.Errorf("inspect central sampler API nullability: %w", err)
	}
	if apiNullable != "NO" {
		return fmt.Errorf("central sampler schema sampler_conversation.api is nullable")
	}
	var apiConstraint bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
		JOIN pg_namespace n ON n.oid=t.relnamespace
		WHERE n.nspname=current_schema() AND t.relname='sampler_conversation'
		AND c.conname='sampler_conversation_api_check'
		AND pg_get_constraintdef(c.oid) LIKE '%responses%'
		AND pg_get_constraintdef(c.oid) LIKE '%anthropic_messages%')`).Scan(&apiConstraint); err != nil {
		return fmt.Errorf("inspect central sampler API constraint: %w", err)
	}
	if !apiConstraint {
		return fmt.Errorf("central sampler schema is missing sampler_conversation_api_check")
	}
	return nil
}
