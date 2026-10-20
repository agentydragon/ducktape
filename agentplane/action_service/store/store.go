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
)

// Channels the store notifies.
const (
	// UpdatesChannel carries the ID of an ActionRequest that gained an Event.
	UpdatesChannel = "agentplane_action_updates"
	// DispatchChannel is notified, with no payload, when an Execution awaits dispatch.
	DispatchChannel = "agentplane_action_dispatches"
)

// GrantAuthority decides whether a Connection's grant still authorizes an Action.
type GrantAuthority interface {
	// AuthorizeAction runs in the store's transaction and must take the Connection's row locks there, so a
	// revocation is atomic with the write that relied on the grant.
	AuthorizeAction(ctx context.Context, tx *sql.Tx, grant ExternalGrant) (bool, error)
}

// Store reads and writes the Action Service's database.
type Store struct {
	db     *sql.DB
	grants GrantAuthority
}

func New(db *sql.DB, grants GrantAuthority) *Store { return &Store{db: db, grants: grants} }

// now is the time a write records: PostgreSQL keeps microseconds, so a value read back equals the one written.
func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func (s *Store) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit()
}

// read runs f on one snapshot, so a request, its Decision, its Execution and its Events agree.
func (s *Store) read(ctx context.Context, f func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	return f(tx)
}

func notify(ctx context.Context, tx *sql.Tx, channel, payload string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, $2)", channel, payload)
	return err
}

// Errors the store distinguishes. Every failure of a request that is not one of these is the database's.
var (
	// ErrNotFound is a missing request, or one the reader may not see.
	ErrNotFound = errors.New("action request not found")
	// ErrInvalid is an argument that breaks a documented bound or pattern.
	ErrInvalid = errors.New("invalid argument")
	// ErrExternalGrantNotAuthorized is a submission whose Connection grant does not authorize its caller.
	ErrExternalGrantNotAuthorized = errors.New("external grant is not authorized")
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
	// ConflictNoPendingDispatch is an allowed request whose Execution does not await dispatch.
	ConflictNoPendingDispatch
)

// ConflictError is a request the state of the request or of the key's history refuses; retrying it unchanged
// fails again.
type ConflictError struct {
	Reason  ConflictReason
	Message string
}

func (e *ConflictError) Error() string { return e.Message }
