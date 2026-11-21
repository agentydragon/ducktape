package inbox

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
)

// LeaseDuration is how long a claim holds an inbox, and so how long a worker's fenced writes can succeed after
// it claimed.
const LeaseDuration = 30 * time.Second

// releaseBackoff is the shortest wait before an inbox is worked again after a failure or while a notice awaits
// its receipts.
const releaseBackoff = 5 * time.Second

// ErrClaimLost is a write by a worker whose claim expired, was taken over, or whose inbox was retired.
var ErrClaimLost = errors.New("inbox claim lost")

var inboxLease = lease.Table{Name: "inbox", Key: []string{"id"}, Holder: "claim", Until: "claim_until"}

// Claim is an inbox a worker holds under a lease. Every write made for a claim is fenced: it fails with
// ErrClaimLost unless the lease is still this worker's.
type Claim struct {
	// Inbox is the inbox as it was when claimed. Its counters move on, so anything that matters is read again
	// by a fenced call.
	Inbox
	held lease.Lease
}

// Holder is the exact Identity persisted on the claimed inbox.
func (c Claim) Holder() Owner {
	return Owner{Identity: c.OwnerIdentity}
}

// LockedInbox is the claimed inbox as a fenced call reads it, locked for update.
type LockedInbox struct {
	Inbox
}

// Claim leases the due inbox that has waited longest, or returns nil when none is due. Replicas never claim the
// same inbox: a row another transaction holds is skipped.
func (s *Store) Claim(ctx context.Context) (*Claim, error) {
	return s.claimDueInbox(ctx, `
			SELECT `+inboxColumns+` FROM inbox
			WHERE NOT retired AND next_attempt <= now() AND claim_until <= now()
			ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED`)
}

// ClaimSource leases the inbox with the oldest due source subscription, or returns nil when no source is due.
// Delivery work has its own deadline in inbox.next_attempt and is not considered here.
func (s *Store) ClaimSource(ctx context.Context) (*Claim, error) {
	return s.claimDueInbox(ctx, `
			SELECT `+inboxColumns+` FROM inbox i
			WHERE NOT retired AND claim_until <= now() AND EXISTS (
				SELECT 1 FROM subscription s
				WHERE s.inbox_id = i.id AND NOT s.cancelled AND s.expires_at > now() AND s.next_attempt <= now()
			)
			ORDER BY (
				SELECT min(s.next_attempt) FROM subscription s
				WHERE s.inbox_id = i.id AND NOT s.cancelled AND s.expires_at > now()
			) LIMIT 1 FOR UPDATE OF i SKIP LOCKED`)
}

func (s *Store) claimDueInbox(ctx context.Context, query string) (*Claim, error) {
	var claim *Claim
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		var found Claim
		inbox, err := scanInbox(tx.QueryRowContext(ctx, query))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		// The locked row was selected with an expired lease. It is still expired at acquisition.
		found.held, err = inboxLease.Acquire(ctx, tx, []any{inbox.ID}, LeaseDuration)
		if err != nil {
			return err
		}
		found.Inbox = inbox
		// Other replicas must account for this lease's expiry.
		if err := notify(ctx, tx); err != nil {
			return err
		}
		claim = &found
		return nil
	})
	return claim, err
}

// FenceClaimTx locks the claimed inbox for update, or fails with ErrClaimLost when the claim no longer holds it.
func FenceClaimTx(ctx context.Context, tx transaction.Tx, claim Claim) (LockedInbox, error) {
	if err := inboxLease.Fence(ctx, tx, claim.held); errors.Is(err, lease.ErrLost) {
		return LockedInbox{}, ErrClaimLost
	} else if err != nil {
		return LockedInbox{}, err
	}
	inbox, err := scanInbox(tx.QueryRowContext(ctx, `
		SELECT `+inboxColumns+` FROM inbox WHERE id = $1 AND NOT retired`, claim.ID))
	if errors.Is(err, sql.ErrNoRows) {
		return LockedInbox{}, ErrClaimLost
	}
	if err != nil {
		return LockedInbox{}, err
	}
	return LockedInbox{Inbox: inbox}, nil
}

// fencedTransaction runs f in a transaction that holds the claimed inbox locked.
func (s *Store) fencedTransaction(ctx context.Context, claim Claim, f func(tx transaction.Tx, held LockedInbox) error) error {
	return s.transaction(ctx, func(tx transaction.Tx) error {
		held, err := FenceClaimTx(ctx, tx, claim)
		if err != nil {
			return err
		}
		return f(tx, held)
	})
}

// Release gives the inbox back and schedules delivery work from the durable state alone: a notice still awaiting
// receipts, or entries no notice covers yet, due at noticeDueAt (the debounce deadline, nil when there is none).
// Source deadlines are scheduled independently by ClaimSource. deliveryError is shown on the inbox until the
// next release replaces it, and a failure postpones delivery work by at least five seconds.
func (s *Store) Release(ctx context.Context, claim Claim, deliveryError *string, noticeDueAt *time.Time) error {
	return s.ReleaseWithRetry(ctx, claim, deliveryError, noticeDueAt, releaseBackoff)
}

// ReleaseWithRetry gives the inbox back with a caller-selected minimum wait after delivery failures.
func (s *Store) ReleaseWithRetry(ctx context.Context, claim Claim, deliveryError *string, noticeDueAt *time.Time, minimumRetry time.Duration) error {
	if minimumRetry < releaseBackoff {
		minimumRetry = releaseBackoff
	}
	return s.fencedTransaction(ctx, claim, func(tx transaction.Tx, held LockedInbox) error {
		released := now()
		notice, err := latestNotice(ctx, tx, held.ID)
		if err != nil {
			return err
		}
		waiting := notice != nil && !notice.Confirmed && notice.Error == nil
		unread := held.LastCursor > max(held.Covered, held.Acknowledged, held.ExpiredThrough)
		backoff := released.Add(minimumRetry)

		var next *time.Time
		earliest := func(deadline time.Time) {
			if next == nil || deadline.Before(*next) {
				next = &deadline
			}
		}
		if waiting {
			earliest(backoff)
		} else if unread {
			if noticeDueAt != nil {
				earliest(*noticeDueAt)
			} else {
				earliest(backoff)
			}
		}
		if deliveryError != nil && next != nil && next.Before(backoff) {
			next = &backoff
		}
		if err := inboxLease.Release(ctx, tx, claim.held); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET delivery_error = $2, next_attempt = $3 WHERE id = $1`,
			held.ID, deliveryError, nullTime(next)); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}

// ReleaseSource gives a source claim back without changing any delivery state or deadline.
func (s *Store) ReleaseSource(ctx context.Context, claim Claim) error {
	return s.fencedTransaction(ctx, claim, func(tx transaction.Tx, _ LockedInbox) error {
		if err := inboxLease.Release(ctx, tx, claim.held); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}

func nullTime(t *time.Time) sql.NullTime {
	if t == nil {
		return sql.NullTime{}
	}
	return sql.NullTime{Time: *t, Valid: true}
}

// NextWorkAt is the earliest durable deadline at which an inbox can be claimed: its retry time, or the end of
// the lease on it, whichever is later. Nil when no inbox has work scheduled. It is a deadline, not an interval
// for polling the queue.
func (s *Store) NextWorkAt(ctx context.Context) (*time.Time, error) {
	return s.nextDeadline(ctx,
		`SELECT min(greatest(next_attempt, claim_until)) FROM inbox WHERE NOT retired AND next_attempt IS NOT NULL`)
}

// NextSourceWorkAt is the earliest durable source deadline after an inbox lease expires. Nil when no live
// subscription has source work scheduled.
func (s *Store) NextSourceWorkAt(ctx context.Context) (*time.Time, error) {
	return s.nextDeadline(ctx, `
		SELECT min(greatest(s.next_attempt, i.claim_until))
		FROM subscription s JOIN inbox i ON i.id = s.inbox_id
		WHERE NOT i.retired AND NOT s.cancelled AND s.expires_at > now() AND s.next_attempt IS NOT NULL`)
}

// nextDeadline reads the one deadline query selects, nil when it selects none.
func (s *Store) nextDeadline(ctx context.Context, query string) (*time.Time, error) {
	var deadline sql.NullTime
	if err := s.db.QueryRowContext(ctx, query).Scan(&deadline); err != nil {
		return nil, err
	}
	if !deadline.Valid {
		return nil, nil
	}
	return utc(deadline.Time), nil
}

// DueSubscription is a subscription whose source is due to be read.
type DueSubscription struct {
	Subscription
	SourceState sourceadapter.DueState
	// Generation moves on each time GitHub ingress wakes the subscription, so that a read made before can tell
	// that it may have missed a delivery.
	Generation int64
}

// Source returns the claimed inbox's subscription whose source has waited longest past its due time, or nil.
// Cancelled and expired subscriptions are never due.
func (s *Store) Source(ctx context.Context, claim Claim) (*DueSubscription, error) {
	var afterSequence, app, installation, repository sql.NullInt64
	var generation int64
	subscription, err := scanSubscription(s.db.QueryRowContext(ctx, `
		SELECT `+subscriptionColumns+`, s.actions_after_sequence, s.generation, s.github_app_id, s.github_installation_id,
			s.github_repository_id FROM subscription s
		WHERE s.inbox_id = $1 AND NOT s.cancelled AND s.expires_at > now() AND s.next_attempt <= now()
		ORDER BY s.next_attempt LIMIT 1`, claim.ID), now(), &afterSequence, &generation, &app, &installation, &repository)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	due := DueSubscription{
		Subscription: subscription,
		SourceState:  sourceadapter.DecodeDueState(subscription.Source, afterSequence, app, installation, repository),
		Generation:   generation,
	}
	return &due, nil
}

// SourceFailed records that the subscription's source could not be processed, and defers its next attempt by
// retryAfter. The failure's start moves only when its kind changes. A subscription that was cancelled, expired
// or renewed since it was read as source (its version moved) keeps its state.
func (s *Store) SourceFailed(ctx context.Context, claim Claim, source Subscription, kind model.SourceFailureKind, message string, retryAfter time.Duration) error {
	return s.fencedTransaction(ctx, claim, func(tx transaction.Tx, held LockedInbox) error {
		failed := now()
		result, err := tx.ExecContext(ctx, `
			UPDATE subscription SET next_attempt = $4,
				error_since = CASE WHEN error_kind IS DISTINCT FROM $5 THEN $3 ELSE error_since END,
				error_kind = $5, error = $6, error_observed_at = $3
			WHERE id = $1 AND inbox_id = $2 AND NOT cancelled AND version = $7 AND expires_at > $3`,
			source.ID, held.ID, failed, failed.Add(retryAfter), kind, message, source.Version)
		if err != nil {
			return err
		}
		return notifyIfChanged(ctx, tx, result)
	})
}

// SourceDeferred postpones the subscription's next attempt to until, without recording a failure.
func (s *Store) SourceDeferred(ctx context.Context, claim Claim, source Subscription, until time.Time) error {
	return s.fencedTransaction(ctx, claim, func(tx transaction.Tx, held LockedInbox) error {
		result, err := tx.ExecContext(ctx,
			`UPDATE subscription SET next_attempt = $3 WHERE id = $1 AND inbox_id = $2 AND NOT cancelled AND version = $4`,
			source.ID, held.ID, until, source.Version)
		if err != nil {
			return err
		}
		return notifyIfChanged(ctx, tx, result)
	})
}

func notifyIfChanged(ctx context.Context, tx transaction.Tx, result sql.Result) error {
	changed, err := result.RowsAffected()
	if err != nil || changed == 0 {
		return err
	}
	return notify(ctx, tx)
}

// EntryTimes bounds when the entries that no notice covers and the agent has not acknowledged were committed.
type EntryTimes struct {
	First, Last time.Time
}

// PendingEntryTimes returns the commit times of the claimed inbox's oldest and newest entries beyond its
// covered, acknowledged and expired cursors, or nil when there are none.
func (s *Store) PendingEntryTimes(ctx context.Context, claim Claim) (*EntryTimes, error) {
	var times *EntryTimes
	err := s.fencedTransaction(ctx, claim, func(tx transaction.Tx, held LockedInbox) error {
		var err error
		times, _, err = entriesBeyond(ctx, tx, held.ID, max(held.Covered, held.Acknowledged, held.ExpiredThrough))
		return err
	})
	return times, err
}
