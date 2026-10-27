// Package store holds the Action Service's ActionRequests, Events, Decisions and Executions in its PostgreSQL
// database.
//
// Every write is one transaction, and a commit that changes what a reader follows sends a NOTIFY in it:
// `UpdatesChannel` with the request's ID for every Event, `DispatchChannel` for every Execution that awaits
// dispatch. A notification carries nothing durable: the rows stay the authority.
package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
)

// Channels the store notifies.
const (
	// UpdatesChannel carries the ID of an ActionRequest that gained an Event.
	UpdatesChannel = "agentplane_action_updates"
	// DispatchChannel is notified, with no payload, when an Execution awaits dispatch.
	DispatchChannel = "agentplane_action_dispatches"
)

// Store reads and writes the Action Service's database.
type Store struct {
	db *sql.DB
}

func New(db *sql.DB) *Store { return &Store{db: db} }

// now is the time a write records: PostgreSQL keeps microseconds, so a value read back equals the one written.
func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func (s *Store) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, f)
}

// read runs f on one snapshot, so a request, its Decision, its Execution and its Events agree.
func (s *Store) read(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.ReadTransaction(ctx, s.db, sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true}, f)
}

func notify(ctx context.Context, tx *sql.Tx, channel, payload string) error {
	return postgres.Notify(ctx, tx, channel, payload)
}

// Errors the store distinguishes. Every failure of a request that is not one of these is the database's.
var (
	// ErrNotFound is a missing request, or one the reader may not see.
	ErrNotFound = errors.New("action request not found")
	// ErrInvalid is an argument that breaks a documented bound or pattern.
	ErrInvalid = errors.New("invalid argument")
)

// ConflictReason is why a request conflicts with the state it meets.
type ConflictReason int

const (
	// ConflictIdempotencyKeyUsed is a submission whose key the caller already spent, whatever else it says.
	ConflictIdempotencyKeyUsed ConflictReason = iota + 1
	// ConflictDecisionKeyReused is a Decision key already used for another request or verdict.
	ConflictDecisionKeyReused
	// ConflictVersionChanged is a Decision made on a version that is no longer the request's.
	ConflictVersionChanged
	// ConflictAlreadyDecided is a Decision on a request that is no longer awaiting one.
	ConflictAlreadyDecided
	// ConflictNotDispatchable is a claim on an Execution that awaits dispatch although its request is not allowed.
	ConflictNotDispatchable
	// ConflictNotStartable is a start of an Execution that is not claimed, or already started.
	ConflictNotStartable
	// ConflictLeaseNotOwned is a completion that does not present the lease granted at claim time.
	ConflictLeaseNotOwned
	// ConflictNotFinishable is a completion of an Execution that is neither in flight nor awaiting reconciliation.
	ConflictNotFinishable
)

// ConflictError is a request the state of the request or of the key's history refuses; retrying it unchanged
// fails again.
type ConflictError struct {
	Reason  ConflictReason
	Message string
}

func (e *ConflictError) Error() string { return e.Message }
