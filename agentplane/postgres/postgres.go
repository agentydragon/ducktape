// Package postgres provides database/sql helpers for Agentplane PostgreSQL services.
package postgres

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	_ "github.com/lib/pq"
)

// Open opens the PostgreSQL URL, applies the required pool limits, and verifies the connection.
func Open(ctx context.Context, databaseURL string, maxOpen, maxIdle int) (*sql.DB, error) {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxIdle)
	if err := db.PingContext(ctx); err != nil {
		return nil, errors.Join(err, db.Close())
	}
	return db, nil
}

// Transaction runs f in one transaction and commits it only when f succeeds.
func Transaction(ctx context.Context, db *sql.DB, options sql.TxOptions, f func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, &options)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit()
}

// NamedTransaction runs a transaction with a fixed operation name visible in
// pg_stat_activity. It logs transactions lasting at least one second, including
// commit or rollback time, so lock-wait logs can be joined by backend PID and time.
// Use a short, static name without user data. Tagging costs one round trip.
func NamedTransaction(ctx context.Context, db *sql.DB, options sql.TxOptions, operation string, f func(*sql.Tx) error) error {
	var started time.Time
	var pid int
	err := Transaction(ctx, db, options, func(tx *sql.Tx) error {
		started = time.Now()
		if err := tx.QueryRowContext(
			ctx,
			`SELECT pg_backend_pid() FROM set_config('application_name', $1, true)`, operation,
		).Scan(&pid); err != nil {
			return fmt.Errorf("name PostgreSQL transaction: %w", err)
		}
		return f(tx)
	})
	ended := time.Now()
	if pid != 0 && ended.Sub(started) >= time.Second {
		slog.WarnContext(
			ctx, "slow PostgreSQL transaction",
			"operation", operation,
			"pg_backend_pid", pid,
			"started_at", started.UTC(),
			"ended_at", ended.UTC(),
			"duration_ms", ended.Sub(started).Milliseconds(),
			"succeeded", err == nil,
		)
	}
	return err
}

// ReadTransaction runs f in a read-only transaction and rolls it back after the read.
func ReadTransaction(ctx context.Context, db *sql.DB, options sql.TxOptions, f func(*sql.Tx) error) error {
	if !options.ReadOnly {
		return errors.New("read transaction requires read-only options")
	}
	tx, err := db.BeginTx(ctx, &options)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	return f(tx)
}

// Notify sends a PostgreSQL notification in tx, with the supplied channel and payload.
func Notify(ctx context.Context, tx interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}, channel, payload string,
) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, $2)", channel, payload)
	return err
}
