// Package transaction owns Notification Service SQL transaction lifecycles.
package transaction

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/postgres"
)

// Runner owns transaction begin, commit and rollback for Notification Service writes.
type Runner struct {
	db *sql.DB
}

func New(db *sql.DB) *Runner { return &Runner{db: db} }

// Tx grants SQL access to the current transaction without exposing its lifecycle or database.
type Tx struct {
	tx *sql.Tx
}

func (tx Tx) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	return tx.tx.ExecContext(ctx, query, args...)
}

func (tx Tx) QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error) {
	return tx.tx.QueryContext(ctx, query, args...)
}

func (tx Tx) QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row {
	return tx.tx.QueryRowContext(ctx, query, args...)
}

func (r *Runner) Run(ctx context.Context, f func(Tx) error) error {
	return postgres.Transaction(ctx, r.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		return f(Tx{tx: tx})
	})
}
