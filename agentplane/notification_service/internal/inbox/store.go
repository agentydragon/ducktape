// Package inbox holds the Notification Service's inboxes, subscriptions and entries in its PostgreSQL database.
//
// Every write is one short transaction, and a write that changes what a worker follows commits a NOTIFY on
// wakeup.WorkChannel. A notification carries nothing: the rows stay the authority.
package inbox

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
)

// Quotas bound what one owner, inbox or subscription set may hold. The service's config holds them under its
// `quotas` key, named by the mapstructure tags.
type Quotas struct {
	// InboxesPerAccount keeps its settings key while limiting inboxes per execution Identity.
	InboxesPerAccount int `mapstructure:"inboxes_per_account"`
	// ActiveSubscriptionsPerInbox is the most subscriptions of an inbox that are neither cancelled nor expired.
	ActiveSubscriptionsPerInbox int `mapstructure:"active_subscriptions_per_inbox"`
	// EntriesPerInbox is the most distinct source-event identities over an inbox's lifetime. Messages are not
	// counted: reaching it ends the inbox's subscriptions.
	EntriesPerInbox int `mapstructure:"entries_per_inbox"`
	// UnacknowledgedMessagesPerSender is the most messages one sender may have in an inbox beyond its
	// acknowledged cursor.
	UnacknowledgedMessagesPerSender int `mapstructure:"unacknowledged_messages_per_sender"`
}

// Owner identifies the Session execution Identity.
type Owner struct {
	Identity model.Identity
}

// ownerPredicate selects inboxes established for the exact execution Identity. A Sandbox is not part
// of inbox ownership: workloads running as the same Identity may manage the Session's inbox.
func ownerPredicate(owner Owner, alias string, first int) (string, []any) {
	return fmt.Sprintf("%[1]sowner_identity_name = $%[2]d AND %[1]sowner_identity_uid = $%[3]d AND $%[2]d <> '' AND $%[3]d <> ''",
			alias, first, first+1),
		[]any{owner.Identity.Name, owner.Identity.UID}
}

// Errors the store distinguishes, one per gRPC status code a caller maps it to. Quota failures are *QuotaError.
var (
	// ErrNotFound is a missing resource, or one that belongs to another account.
	ErrNotFound = errors.New("resource not found or not owned by this account")
	// ErrIdempotencyKeyReused is a creation key that already names a subscription or message made with other
	// arguments.
	ErrIdempotencyKeyReused = errors.New("idempotency key already names another subscription or message")
	// ErrVersionChanged is a renewal of a subscription whose version moved on, or that is cancelled.
	ErrVersionChanged = errors.New("subscription version changed or subscription cancelled")
	// ErrInboxRetired is a change to an inbox that was retired.
	ErrInboxRetired = errors.New("inbox is retired")
	// ErrCursorOutOfRange is an acknowledgement beyond the inbox's last committed entry.
	ErrCursorOutOfRange = errors.New("cursor is beyond the committed prefix")
)

// Quota names a limit in Quotas.
type Quota int

const (
	InboxesPerAccount Quota = iota + 1
	ActiveSubscriptionsPerInbox
	EntriesPerInbox
	UnacknowledgedMessagesPerSender
)

// QuotaError is a write that would pass a quota.
type QuotaError struct {
	Quota Quota
	Limit int
}

func (e *QuotaError) Error() string {
	switch e.Quota {
	case InboxesPerAccount:
		return fmt.Sprintf("%d inboxes per Identity", e.Limit)
	case ActiveSubscriptionsPerInbox:
		return fmt.Sprintf("%d active subscriptions per inbox", e.Limit)
	case UnacknowledgedMessagesPerSender:
		return fmt.Sprintf("%d unacknowledged messages per sender in an inbox; wait for the recipient to acknowledge", e.Limit)
	default:
		return fmt.Sprintf("inbox has reached its %d-entry lifetime limit; retire it explicitly", e.Limit)
	}
}

// Store reads and writes the Notification Service's database.
type Store struct {
	db     *sql.DB
	runner *transaction.Runner
	quotas Quotas
}

func New(db *sql.DB, runner *transaction.Runner, quotas Quotas) *Store {
	return &Store{db: db, runner: runner, quotas: quotas}
}

// now is the time a write records: PostgreSQL keeps microseconds, so a value read back equals the one written.
func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func (s *Store) transaction(ctx context.Context, f func(transaction.Tx) error) error {
	return s.runner.Run(ctx, f)
}

// startAfter is the ID a page of an ID-ordered list starts after: the nil UUID, which no row has, for the first.
func startAfter(id *uuid.UUID) uuid.UUID {
	if id == nil {
		return uuid.Nil
	}
	return *id
}

func notify(ctx context.Context, tx transaction.Tx) error {
	return postgres.Notify(ctx, tx, wakeup.WorkChannel, "")
}
