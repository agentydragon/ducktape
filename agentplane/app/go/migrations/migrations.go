package migrations

import (
	"context"
	"database/sql"
	"embed"
	"errors"
	"fmt"
	"io/fs"
	"time"

	"github.com/pressly/goose/v3"
)

//go:embed app/*.sql
var scripts embed.FS

const migrationLockID int64 = 0x5452_414A

// Up applies the current schema baseline and subsequent migrations.
func Up(ctx context.Context, db *sql.DB) (retErr error) {
	conn, err := db.Conn(ctx)
	if err != nil {
		return fmt.Errorf("get app migration lock connection: %w", err)
	}
	if _, err := conn.ExecContext(ctx, `SELECT pg_advisory_lock($1)`, migrationLockID); err != nil {
		_ = conn.Close()
		return fmt.Errorf("acquire app migration advisory lock: %w", err)
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := conn.ExecContext(unlockCtx, `SELECT pg_advisory_unlock($1)`, migrationLockID); err != nil {
			retErr = errors.Join(retErr, fmt.Errorf("release app migration advisory lock: %w", err))
		}
		if err := conn.Close(); err != nil {
			retErr = errors.Join(retErr, fmt.Errorf("close app migration lock connection: %w", err))
		}
	}()

	if err := preflight(ctx, conn); err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "app")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("agentplane_app_goose_version"),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

// preflight rejects a non-empty, unknown, or partial app schema before Goose creates its metadata
// table. The baseline repeats these checks while holding the migration advisory lock, so a schema
// change after this read-only check still cannot be adopted accidentally.
type queryRower interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func preflight(ctx context.Context, db queryRower) error {
	var hasAlembicVersion bool
	if err := db.QueryRowContext(ctx, `SELECT to_regclass('alembic_version_app') IS NOT NULL`).Scan(&hasAlembicVersion); err != nil {
		return fmt.Errorf("inspect app Alembic marker: %w", err)
	}
	if !hasAlembicVersion {
		var hasPartialSchema bool
		if err := db.QueryRowContext(ctx, `
			SELECT EXISTS (
				SELECT 1
				  FROM unnest(ARRAY[
					'thread', 'operator_browser_session', 'event', 'feed_state', 'event_log', 'sandbox_ingestion'
				  ]) AS candidate(name)
				 WHERE to_regclass(candidate.name) IS NOT NULL
			)
		`).Scan(&hasPartialSchema); err != nil {
			return fmt.Errorf("inspect unmarked app schema: %w", err)
		}
		if hasPartialSchema {
			return errors.New("refusing to initialize app schema without alembic_version_app; app tables already exist")
		}
		return nil
	}

	var revisionCount int
	var revision sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT count(*), min(version_num) FROM alembic_version_app`).Scan(&revisionCount, &revision); err != nil {
		return fmt.Errorf("read app Alembic marker: %w", err)
	}
	if revisionCount != 1 || !revision.Valid || revision.String != "0036_auto_thread_titles" {
		return fmt.Errorf("refusing to adopt app schema at Alembic revision %q (expected exactly 0036_auto_thread_titles)", revision.String)
	}

	var incompatibleColumns int
	if err := db.QueryRowContext(ctx, `
		SELECT count(*)
		  FROM (VALUES
			('thread', 'id', 'uuid', 'NO'),
			('thread', 'name', 'text', 'YES'),
			('thread', 'archived', 'boolean', 'NO'),
			('thread', 'created_at', 'timestamp with time zone', 'NO'),
			('thread', 'auto_title_on_first_message', 'boolean', 'NO'),
			('thread', 'title_user_managed', 'boolean', 'NO'),
			('thread', 'title_status', 'text', 'NO'),
			('thread', 'title_scan_cursor', 'bigint', 'NO'),
			('thread', 'title_claimed_at', 'timestamp with time zone', 'YES'),
			('thread', 'title_revision', 'integer', 'NO'),
			('operator_browser_session', 'id', 'text', 'NO'),
			('operator_browser_session', 'expires_at', 'timestamp with time zone', 'NO'),
			('operator_browser_session', 'payload', 'jsonb', 'NO'),
			('operator_browser_session', 'absolute_expires_at', 'timestamp with time zone', 'NO'),
			('operator_browser_session', 'operator_issuer', 'text', 'YES'),
			('operator_browser_session', 'operator_subject', 'text', 'YES'),
			('operator_browser_session', 'operator_username', 'text', 'YES'),
			('operator_browser_session', 'access_token', 'text', 'YES'),
			('operator_browser_session', 'access_token_expires_at', 'timestamp with time zone', 'YES'),
			('operator_browser_session', 'refresh_token', 'text', 'YES')
		  ) AS expected(table_name, column_name, data_type, is_nullable)
		  LEFT JOIN information_schema.columns AS actual
		    ON actual.table_schema = current_schema()
		   AND actual.table_name = expected.table_name
		   AND actual.column_name = expected.column_name
		 WHERE actual.column_name IS NULL
		    OR actual.data_type <> expected.data_type
		    OR actual.is_nullable <> expected.is_nullable
	`).Scan(&incompatibleColumns); err != nil {
		return fmt.Errorf("inspect app schema columns: %w", err)
	}
	if incompatibleColumns != 0 {
		return errors.New("refusing to adopt app schema with missing or incompatible columns")
	}

	for _, table := range []string{"thread", "operator_browser_session"} {
		var hasExpectedPrimaryKey bool
		if err := db.QueryRowContext(ctx, `
			SELECT EXISTS (
				SELECT 1 FROM pg_constraint AS c
				 WHERE c.conrelid = to_regclass($1) AND c.contype = 'p'
				   AND (
					 SELECT array_agg(a.attname::text ORDER BY key.ordinality)
					   FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ordinality)
					   JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = key.attnum
				   ) = ARRAY['id']::text[]
			)
		`, table).Scan(&hasExpectedPrimaryKey); err != nil {
			return fmt.Errorf("inspect %s primary key: %w", table, err)
		}
		if !hasExpectedPrimaryKey {
			return fmt.Errorf("refusing to adopt app schema with incompatible %s primary key", table)
		}
		var foreignKeyCount int
		if err := db.QueryRowContext(ctx, `
			SELECT count(*) FROM pg_constraint WHERE conrelid = to_regclass($1) AND contype = 'f'
		`, table).Scan(&foreignKeyCount); err != nil {
			return fmt.Errorf("inspect %s foreign keys: %w", table, err)
		}
		if foreignKeyCount != 0 {
			return fmt.Errorf("refusing to adopt app schema with unexpected %s foreign keys", table)
		}
	}

	var incompatibleChecks int
	if err := db.QueryRowContext(ctx, `
		SELECT count(*)
		  FROM (VALUES
			('operator_browser_session_operator_whole', 'operator_issuerisnull=operator_subjectisnullandoperator_issuerisnull=operator_usernameisnull'),
			('operator_browser_session_access_token_whole', 'access_tokenisnull=access_token_expires_atisnull'),
			('operator_browser_session_tokens_logged_in', 'access_tokenisnulloroperator_issuerisnotnull'),
			('operator_browser_session_refresh_token_with_access_token', 'refresh_tokenisnulloraccess_tokenisnotnull')
		  ) AS expected(name, expression)
	  LEFT JOIN pg_constraint AS actual
	    ON actual.conrelid = 'operator_browser_session'::regclass
	   AND actual.contype = 'c'
	   AND actual.conname = expected.name
	 WHERE actual.oid IS NULL
	    OR regexp_replace(lower(pg_get_expr(actual.conbin, actual.conrelid)), '[[:space:]()]', '', 'g') <> expected.expression
	`).Scan(&incompatibleChecks); err != nil {
		return fmt.Errorf("inspect app schema checks: %w", err)
	}
	if incompatibleChecks != 0 {
		return errors.New("refusing to adopt app schema with missing or incompatible check constraints")
	}

	var hasExpiryIndex bool
	if err := db.QueryRowContext(ctx, `
		SELECT EXISTS (
			SELECT 1
			  FROM pg_index AS i
			  JOIN pg_class AS idx ON idx.oid = i.indexrelid
			  JOIN pg_am AS am ON am.oid = idx.relam
			  JOIN pg_attribute AS indexed_column
			    ON indexed_column.attrelid = i.indrelid AND indexed_column.attnum = i.indkey[0]
			 WHERE i.indrelid = 'operator_browser_session'::regclass
			   AND idx.relname = 'ix_operator_browser_session_expires_at'
			   AND i.indisvalid AND i.indisready AND NOT i.indisunique
			   AND i.indnkeyatts = 1 AND i.indnatts = 1
			   AND indexed_column.attname = 'expires_at'
			   AND am.amname = 'btree'
		)
	`).Scan(&hasExpiryIndex); err != nil {
		return fmt.Errorf("inspect operator session expiry index: %w", err)
	}
	if !hasExpiryIndex {
		return errors.New("refusing to adopt app schema with missing or incompatible expiry index")
	}
	return nil
}
