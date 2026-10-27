// Package postgres provides database/sql helpers for Agentplane PostgreSQL services.
package postgres

import (
	"context"
	"database/sql"
	"errors"

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
func Notify(ctx context.Context, tx *sql.Tx, channel, payload string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, $2)", channel, payload)
	return err
}
