// Package migrations owns the guarded Goose handoff for the Dispatcher database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed dispatcher/*.sql
var scripts embed.FS

type column struct {
	typ      string
	notNull  bool
	identity bool
}

type index struct {
	columns string
	unique  bool
}

type schema struct {
	head    string
	lockID  int64
	version string
	columns map[string]map[string]column
	indexes map[string]map[string]index
}

var dispatcherSchema = schema{
	head: "0007_drop_opening_attached", lockID: 0x4453_5043, version: "runner_gateway_dispatcher_goose_version",
	columns: map[string]map[string]column{
		"dispatch": {
			"position": {"int8", true, true}, "command_id": {"varchar", true, false},
			"session_id": {"varchar", true, false}, "sandbox_name": {"varchar", true, false},
			"sandbox_uid": {"uuid", true, false}, "operation": {"varchar", true, false},
			"command": {"bytea", true, false}, "state": {"varchar", true, false},
			"reason": {"varchar", false, false}, "admission": {"bytea", false, false},
			"created_at": {"timestamptz", true, false}, "updated_at": {"timestamptz", true, false},
			"wake_eligible": {"bool", true, false}, "wake_from_incarnation": {"varchar", false, false},
			"wake_caller_namespace": {"varchar", false, false}, "wake_caller_name": {"varchar", false, false},
			"wake_requested_at": {"timestamptz", false, false},
		},
		"session_opening": {
			"session_id": {"varchar", true, false}, "sandbox_name": {"varchar", true, false},
			"sandbox_uid": {"uuid", true, false}, "spec": {"bytea", true, false},
			"setup_script": {"varchar", false, false}, "state": {"varchar", true, false},
			"reason":     {"varchar", false, false},
			"created_at": {"timestamptz", true, false}, "updated_at": {"timestamptz", true, false},
			"wake_eligible": {"bool", true, false}, "wake_from_incarnation": {"varchar", false, false},
			"wake_caller_namespace": {"varchar", false, false}, "wake_caller_name": {"varchar", false, false},
			"wake_requested_at": {"timestamptz", false, false},
		},
		"sandbox_ownership": {
			"sandbox_uid": {"uuid", true, false}, "generation": {"int8", true, false},
			"lease_uid": {"varchar", true, false}, "lease_transitions": {"int4", true, false},
			"holder_identity": {"varchar", true, false}, "updated_at": {"timestamptz", true, false},
		},
		"wake_attempt": {
			"caller_namespace": {"varchar", true, false}, "caller_name": {"varchar", true, false},
			"sandbox_uid": {"uuid", true, false}, "wake_generation": {"int8", true, false},
			"attempted_at": {"timestamptz", true, false},
		},
	},
	indexes: map[string]map[string]index{
		"dispatch": {
			"dispatch_pkey": {"position", true}, "dispatch_command_id_key": {"command_id", true},
			"ix_dispatch_session_state":     {"session_id,state", false},
			"ix_dispatch_sandbox_uid_state": {"sandbox_uid,state", false},
			"ix_dispatch_sandbox_name":      {"sandbox_name", false},
		},
		"session_opening": {
			"session_opening_pkey":           {"session_id", true},
			"ix_session_opening_sandbox_uid": {"sandbox_uid", false},
		},
		"sandbox_ownership": {"sandbox_ownership_pkey": {"sandbox_uid", true}},
		"wake_attempt": {
			"wake_attempt_pkey":           {"caller_namespace,caller_name,sandbox_uid,wake_generation", true},
			"ix_wake_attempt_caller_time": {"caller_namespace,caller_name,attempted_at", false},
		},
	},
}

// Up applies authored SQL with Goose. It is a dedicated job entry point, never a
// side effect of serving traffic. It uses the same advisory key as each Alembic
// runner, so a handoff cannot race the previous migration owner.
func Up(ctx context.Context, db *sql.DB) error {
	s := dispatcherSchema
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(s.lockID))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "dispatcher")
	if err != nil {
		return err
	}
	preflight := goose.NewGoMigration(1, &goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		return verifyBaseline(ctx, tx, s)
	}}, nil)
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName(s.version), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true), goose.WithGoMigrations(preflight))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

func verifyBaseline(ctx context.Context, tx *sql.Tx, s schema) error {
	var hasAlembic bool
	if err := tx.QueryRowContext(
		ctx,
		"SELECT to_regclass(format('%I.%I', current_schema(), 'alembic_version')) IS NOT NULL",
	).Scan(&hasAlembic); err != nil {
		return err
	}
	if !hasAlembic {
		for table := range s.columns {
			var exists bool
			if err := tx.QueryRowContext(
				ctx,
				"SELECT to_regclass(format('%I.%I', current_schema(), $1::text)) IS NOT NULL", table,
			).Scan(&exists); err != nil {
				return err
			}
			if exists {
				return fmt.Errorf("%s exists without Alembic history; refusing Goose baseline", table)
			}
		}
		return nil // genuinely fresh owner schema
	}
	var count int
	var minimum, maximum sql.NullString
	if err := tx.QueryRowContext(
		ctx,
		"SELECT count(*), min(version_num), max(version_num) FROM alembic_version",
	).Scan(&count, &minimum, &maximum); err != nil {
		return err
	}
	if count != 1 || minimum.String != s.head || maximum.String != s.head {
		return fmt.Errorf("expected sole Alembic head %q; found %d rows, range %q..%q", s.head, count, minimum.String, maximum.String)
	}
	return verifySchema(ctx, tx, s)
}

func verifySchema(ctx context.Context, tx *sql.Tx, s schema) error {
	actualColumns := make(map[string]map[string]column)
	rows, err := tx.QueryContext(ctx, `
SELECT table_name, column_name,
       udt_name || CASE WHEN character_maximum_length IS NOT NULL
                       THEN '(' || character_maximum_length::text || ')' ELSE '' END,
       is_nullable, is_identity
FROM information_schema.columns WHERE table_schema = current_schema()`)
	if err != nil {
		return err
	}
	for rows.Next() {
		var table, name, typ, nullable, identity string
		if err := rows.Scan(&table, &name, &typ, &nullable, &identity); err != nil {
			rows.Close()
			return err
		}
		if _, owned := s.columns[table]; owned {
			if actualColumns[table] == nil {
				actualColumns[table] = make(map[string]column)
			}
			actualColumns[table][name] = column{typ, nullable == "NO", identity == "YES"}
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	for table, expected := range s.columns {
		if len(actualColumns[table]) != len(expected) {
			return fmt.Errorf("%s column count differs from Alembic baseline", table)
		}
		for name, want := range expected {
			if got, ok := actualColumns[table][name]; !ok || got != want {
				return fmt.Errorf("%s.%s column differs from Alembic baseline: got %+v, expected %+v", table, name, got, want)
			}
		}
	}
	actualIndexes := make(map[string]map[string]index)
	rows, err = tx.QueryContext(ctx, `
SELECT t.relname, i.relname, x.indisunique,
       (SELECT string_agg(a.attname, ',' ORDER BY k.ordinality)
        FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ordinality)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE k.ordinality <= x.indnkeyatts)
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = current_schema()`)
	if err != nil {
		return err
	}
	for rows.Next() {
		var table, name, columns string
		var unique bool
		if err := rows.Scan(&table, &name, &unique, &columns); err != nil {
			rows.Close()
			return err
		}
		if _, owned := s.indexes[table]; owned {
			if actualIndexes[table] == nil {
				actualIndexes[table] = make(map[string]index)
			}
			actualIndexes[table][name] = index{columns, unique}
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	for table, expected := range s.indexes {
		if len(actualIndexes[table]) != len(expected) {
			return fmt.Errorf("%s index count differs from Alembic baseline", table)
		}
		for name, want := range expected {
			if got, ok := actualIndexes[table][name]; !ok || got != want {
				return fmt.Errorf("%s.%s index differs from Alembic baseline: got %+v, expected %+v", table, name, got, want)
			}
		}
	}
	return nil
}
