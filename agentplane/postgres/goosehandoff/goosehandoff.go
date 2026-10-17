// Package goosehandoff moves a service database from Alembic to Goose, or creates it fresh, without
// ever stamping a schema it has not checked.
//
// A service's Goose scripts are an idempotent baseline (`CREATE ... IF NOT EXISTS`) preceded by a
// preflight migration. The preflight refuses a database that has owned tables but no Alembic history,
// and one whose Alembic head or schema is not the one the baseline reproduces, so the baseline either
// creates every owned object or finds exactly what it would have created.
package goosehandoff

import (
	"context"
	"database/sql"
	"fmt"
	"io/fs"
	"slices"

	"github.com/lib/pq"
	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

// Column is a column as `information_schema.columns` reports it.
type Column struct {
	// Type is the `udt_name`, with the character length in parentheses where one is declared.
	Type     string
	NotNull  bool
	Identity bool
}

// Required is a `NOT NULL` column.
func Required(typ string) Column { return Column{Type: typ, NotNull: true} }

// Nullable is a column that admits `NULL`.
func Nullable(typ string) Column { return Column{Type: typ} }

// Identity is a `NOT NULL` identity column.
func Identity(typ string) Column { return Column{Type: typ, NotNull: true, Identity: true} }

// Index is an index's key columns in order, comma-separated, and whether it is unique.
type Index struct {
	Columns string
	Unique  bool
}

// Unique is a unique index on the comma-separated key columns.
func Unique(columns string) Index { return Index{Columns: columns, Unique: true} }

// NonUnique is a non-unique index on the comma-separated key columns.
func NonUnique(columns string) Index { return Index{Columns: columns} }

// Schema is the Alembic head a service hands off from and the objects its baseline script owns.
type Schema struct {
	// Head is the one Alembic revision a database must sit at to be adopted.
	Head string
	// AlembicTable is the Alembic version table.
	AlembicTable string
	// GooseTable is the Goose version table.
	GooseTable string
	// LockID is the advisory-lock key the service's Alembic runner takes; Goose takes the same one, so
	// a handoff cannot race the previous migration owner.
	LockID int64
	// Columns and Indexes are keyed by owned table, then by column or index name. Both must match
	// exactly for every owned table.
	Columns map[string]map[string]Column
	Indexes map[string]map[string]Index
	// Checks lists the CHECK constraint names of the tables it names; a table absent here is not
	// checked. The constraint expression is not compared.
	Checks map[string][]string
}

// Up applies the scripts under `scripts` with Goose. It is a dedicated job entry point, never a side
// effect of serving traffic.
func Up(ctx context.Context, db *sql.DB, scripts fs.FS, s Schema) error {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(s.LockID))
	if err != nil {
		return err
	}
	preflight := goose.NewGoMigration(1, &goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		return verifyBaseline(ctx, tx, s)
	}}, nil)
	provider, err := goose.NewProvider(goose.DialectPostgres, db, scripts,
		goose.WithTableName(s.GooseTable), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true), goose.WithGoMigrations(preflight))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

func verifyBaseline(ctx context.Context, tx *sql.Tx, s Schema) error {
	hasAlembic, err := tableExists(ctx, tx, s.AlembicTable)
	if err != nil {
		return err
	}
	if !hasAlembic {
		for table := range s.Columns {
			exists, err := tableExists(ctx, tx, table)
			if err != nil {
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
		"SELECT count(*), min(version_num), max(version_num) FROM "+pq.QuoteIdentifier(s.AlembicTable),
	).Scan(&count, &minimum, &maximum); err != nil {
		return err
	}
	if count != 1 || minimum.String != s.Head || maximum.String != s.Head {
		return fmt.Errorf("expected sole Alembic head %q; found %d rows, range %q..%q", s.Head, count, minimum.String, maximum.String)
	}
	if err := verifyColumns(ctx, tx, s); err != nil {
		return err
	}
	if err := verifyIndexes(ctx, tx, s); err != nil {
		return err
	}
	return verifyChecks(ctx, tx, s)
}

func tableExists(ctx context.Context, tx *sql.Tx, table string) (bool, error) {
	var exists bool
	err := tx.QueryRowContext(
		ctx,
		"SELECT to_regclass(format('%I.%I', current_schema(), $1::text)) IS NOT NULL", table,
	).Scan(&exists)
	return exists, err
}

func verifyColumns(ctx context.Context, tx *sql.Tx, s Schema) error {
	actual := make(map[string]map[string]Column)
	rows, err := tx.QueryContext(ctx, `
SELECT table_name, column_name,
       udt_name || CASE WHEN character_maximum_length IS NOT NULL
                       THEN '(' || character_maximum_length::text || ')' ELSE '' END,
       is_nullable, is_identity
FROM information_schema.columns WHERE table_schema = current_schema()`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var table, name, typ, nullable, identity string
		if err := rows.Scan(&table, &name, &typ, &nullable, &identity); err != nil {
			return err
		}
		if _, owned := s.Columns[table]; owned {
			if actual[table] == nil {
				actual[table] = make(map[string]Column)
			}
			actual[table][name] = Column{typ, nullable == "NO", identity == "YES"}
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for table, expected := range s.Columns {
		if len(actual[table]) != len(expected) {
			return fmt.Errorf("%s column count differs from Alembic baseline", table)
		}
		for name, want := range expected {
			if got, ok := actual[table][name]; !ok || got != want {
				return fmt.Errorf("%s.%s column differs from Alembic baseline: got %+v, expected %+v", table, name, got, want)
			}
		}
	}
	return nil
}

func verifyIndexes(ctx context.Context, tx *sql.Tx, s Schema) error {
	actual := make(map[string]map[string]Index)
	rows, err := tx.QueryContext(ctx, `
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
	defer rows.Close()
	for rows.Next() {
		var table, name, columns string
		var unique bool
		if err := rows.Scan(&table, &name, &unique, &columns); err != nil {
			return err
		}
		if _, owned := s.Indexes[table]; owned {
			if actual[table] == nil {
				actual[table] = make(map[string]Index)
			}
			actual[table][name] = Index{columns, unique}
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for table, expected := range s.Indexes {
		if len(actual[table]) != len(expected) {
			return fmt.Errorf("%s index count differs from Alembic baseline", table)
		}
		for name, want := range expected {
			if got, ok := actual[table][name]; !ok || got != want {
				return fmt.Errorf("%s.%s index differs from Alembic baseline: got %+v, expected %+v", table, name, got, want)
			}
		}
	}
	return nil
}

func verifyChecks(ctx context.Context, tx *sql.Tx, s Schema) error {
	actual := make(map[string][]string)
	rows, err := tx.QueryContext(ctx, `
SELECT t.relname, c.conname
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE c.contype = 'c' AND n.nspname = current_schema()`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var table, name string
		if err := rows.Scan(&table, &name); err != nil {
			return err
		}
		if _, checked := s.Checks[table]; checked {
			actual[table] = append(actual[table], name)
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for table, expected := range s.Checks {
		got := slices.Sorted(slices.Values(actual[table]))
		if !slices.Equal(got, slices.Sorted(slices.Values(expected))) {
			return fmt.Errorf("%s check constraint set differs from Alembic baseline: got %v, expected %v", table, got, expected)
		}
	}
	return nil
}
