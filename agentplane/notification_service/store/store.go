// Package store holds the Notification Service's inboxes, subscriptions and entries in its PostgreSQL database.
//
// Every write is one short transaction, and a write that changes what a worker follows commits a NOTIFY on
// WorkChannel. A notification carries nothing: the rows stay the authority.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// WorkChannel is the NOTIFY channel that tells workers inbox work was committed.
const WorkChannel = "agentplane_notification_work"

// Quotas bound what one owner, inbox or subscription set may hold.
type Quotas struct {
	// InboxesPerAccount is the most inboxes one owning ServiceAccount retains.
	InboxesPerAccount int
	// ActiveSubscriptionsPerInbox is the most subscriptions of an inbox that are neither cancelled nor expired.
	ActiveSubscriptionsPerInbox int
	// EntriesPerInbox is the most distinct source-event identities over an inbox's lifetime.
	EntriesPerInbox int
}

// ServiceAccount is the owner of inboxes and subscriptions: the account a caller's workloads run as.
type ServiceAccount struct {
	Namespace string
	Name      string
}

// Errors the store distinguishes, one per gRPC status code a caller maps it to. Quota failures are *QuotaError.
var (
	// ErrNotFound is a missing resource, or one that belongs to another account.
	ErrNotFound = errors.New("resource not found or not owned by this account")
	// ErrIdempotencyKeyReused is a creation key that already names a subscription made with other arguments.
	ErrIdempotencyKeyReused = errors.New("idempotency key already names another subscription")
	// ErrVersionChanged is a renewal of a subscription whose version moved on, or that is cancelled.
	ErrVersionChanged = errors.New("subscription version changed or subscription cancelled")
	// ErrInboxRetired is a change to an inbox that was retired.
	ErrInboxRetired = errors.New("inbox is retired")
	// ErrCursorOutOfRange is an acknowledgement beyond the inbox's last committed entry.
	ErrCursorOutOfRange = errors.New("cursor is beyond the committed prefix")
	// ErrUnsupportedSource is a source this store cannot subscribe to yet.
	ErrUnsupportedSource = errors.New("unsupported subscription source")
)

// Quota names a limit in Quotas.
type Quota int

const (
	InboxesPerAccount Quota = iota + 1
	ActiveSubscriptionsPerInbox
	EntriesPerInbox
)

// QuotaError is a write that would pass a quota.
type QuotaError struct {
	Quota Quota
	Limit int
}

func (e *QuotaError) Error() string {
	switch e.Quota {
	case InboxesPerAccount:
		return fmt.Sprintf("%d inboxes per owning ServiceAccount", e.Limit)
	case ActiveSubscriptionsPerInbox:
		return fmt.Sprintf("%d active subscriptions per inbox", e.Limit)
	default:
		return fmt.Sprintf("inbox has reached its %d-entry lifetime limit; retire it explicitly", e.Limit)
	}
}

// Store reads and writes the Notification Service's database.
type Store struct {
	db     *sql.DB
	quotas Quotas
}

func New(db *sql.DB, quotas Quotas) *Store { return &Store{db: db, quotas: quotas} }

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

// startAfter is the ID a page of an ID-ordered list starts after: the nil UUID, which no row has, for the first.
func startAfter(id *uuid.UUID) uuid.UUID {
	if id == nil {
		return uuid.Nil
	}
	return *id
}

func notify(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, '')", WorkChannel)
	return err
}
