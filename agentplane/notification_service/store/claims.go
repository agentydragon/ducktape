package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/lease"
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
	Owner model.ServiceAccount
	held  lease.Lease
	// staleCheckAt is the deadline of the recheck of an unavailable destination, as it was when claimed.
	staleCheckAt *time.Time
}

// heldInbox is the claimed inbox as a fenced call reads it, locked for update.
type heldInbox struct {
	Inbox
	staleCheckAt *time.Time
}

// Claim leases the due inbox that has waited longest, or returns nil when none is due. Replicas never claim the
// same inbox: a row another transaction holds is skipped.
func (s *Store) Claim(ctx context.Context) (*Claim, error) {
	var claim *Claim
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var found Claim
		var staleCheckAt sql.NullTime
		inbox, err := scanInbox(tx.QueryRowContext(ctx, `
			SELECT `+inboxColumns+`, owner_namespace, owner_name, stale_check_at FROM inbox
			WHERE NOT retired AND next_attempt <= now() AND claim_until <= now()
			ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED`),
			&found.Owner.Namespace, &found.Owner.Name, &staleCheckAt)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		found.held, err = inboxLease.Acquire(ctx, tx, []any{inbox.ID}, LeaseDuration)
		// The selection's claim_until <= now() holds at the later clock_timestamp() too, so no lease is held.
		if err != nil {
			return err
		}
		found.Inbox = inbox
		if staleCheckAt.Valid {
			found.staleCheckAt = utc(staleCheckAt.Time)
		}
		// Other replicas must account for this lease's expiry.
		if err := notify(ctx, tx); err != nil {
			return err
		}
		claim = &found
		return nil
	})
	return claim, err
}

// fenced locks the claimed inbox for update, or fails with ErrClaimLost when the claim no longer holds it.
func fenced(ctx context.Context, tx *sql.Tx, claim Claim) (heldInbox, error) {
	if err := inboxLease.Fence(ctx, tx, claim.held); errors.Is(err, lease.ErrLost) {
		return heldInbox{}, ErrClaimLost
	} else if err != nil {
		return heldInbox{}, err
	}
	var staleCheckAt sql.NullTime
	inbox, err := scanInbox(tx.QueryRowContext(ctx, `
		SELECT `+inboxColumns+`, stale_check_at FROM inbox WHERE id = $1 AND NOT retired`, claim.ID), &staleCheckAt)
	if errors.Is(err, sql.ErrNoRows) {
		return heldInbox{}, ErrClaimLost
	}
	if err != nil {
		return heldInbox{}, err
	}
	held := heldInbox{Inbox: inbox}
	if staleCheckAt.Valid {
		held.staleCheckAt = utc(staleCheckAt.Time)
	}
	return held, nil
}

// fencedTransaction runs f in a transaction that holds the claimed inbox locked.
func (s *Store) fencedTransaction(ctx context.Context, claim Claim, f func(tx *sql.Tx, held heldInbox) error) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		held, err := fenced(ctx, tx, claim)
		if err != nil {
			return err
		}
		return f(tx, held)
	})
}

// Release gives the inbox back and schedules its next work from the durable state alone: the earliest due
// subscription, a notice still awaiting receipts, or entries no notice covers yet, due at noticeDueAt (the
// debounce deadline, nil when there is none). deliveryError is shown on the inbox until the next release
// replaces it, and a failure postpones the next work by at least five seconds.
func (s *Store) Release(ctx context.Context, claim Claim, deliveryError *string, noticeDueAt *time.Time) error {
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		released := now()
		var nextSource sql.NullTime
		if err := tx.QueryRowContext(ctx, `
			SELECT min(next_attempt) FROM subscription WHERE inbox_id = $1 AND NOT cancelled AND expires_at > now()`,
			held.ID).Scan(&nextSource); err != nil {
			return err
		}
		notice, err := latestNotice(ctx, tx, held.ID)
		if err != nil {
			return err
		}
		waiting := notice != nil && !notice.Confirmed && notice.Error == nil
		unread := held.LastCursor > max(held.Covered, held.Acknowledged, held.ExpiredThrough)
		backoff := released.Add(releaseBackoff)

		var next *time.Time
		earliest := func(deadline time.Time) {
			if next == nil || deadline.Before(*next) {
				next = &deadline
			}
		}
		if nextSource.Valid {
			earliest(nextSource.Time)
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
		if held.staleCheckAt != nil {
			// A transient lookup failure must not confirm staleness or spin before the recheck.
			floor := released
			if deliveryError != nil {
				floor = backoff
			}
			recheck := *held.staleCheckAt
			if recheck.Before(floor) {
				recheck = floor
			}
			next = &recheck
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
	var deadline sql.NullTime
	if err := s.db.QueryRowContext(ctx,
		`SELECT min(greatest(next_attempt, claim_until)) FROM inbox WHERE NOT retired AND next_attempt IS NOT NULL`).
		Scan(&deadline); err != nil {
		return nil, err
	}
	if !deadline.Valid {
		return nil, nil
	}
	return utc(deadline.Time), nil
}

// ObserveStale records that the claimed inbox's destination is not the Sandbox it names. The first
// observation sets a recheck deadline confirmation from now and releases the claim for it. An observation
// after that deadline retires the inbox, cancels its subscriptions and reports true.
func (s *Store) ObserveStale(ctx context.Context, claim Claim, reason string, confirmation time.Duration) (bool, error) {
	var retired bool
	err := s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		observed := now()
		recheck := observed.Add(confirmation)
		if held.staleCheckAt != nil {
			recheck = *held.staleCheckAt
		}
		if err := inboxLease.Release(ctx, tx, claim.held); err != nil {
			return err
		}
		if held.staleCheckAt != nil && !held.staleCheckAt.After(observed) {
			if _, err := tx.ExecContext(ctx, `
				UPDATE inbox SET retired = true, updated_at = $2, next_attempt = NULL, delivery_error = $3 WHERE id = $1`,
				held.ID, observed, "destination retired: "+reason); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx,
				`UPDATE subscription SET cancelled = true, version = version + 1 WHERE inbox_id = $1 AND NOT cancelled`,
				held.ID); err != nil {
				return err
			}
			retired = true
			return notify(ctx, tx)
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE inbox SET stale_check_at = $2, next_attempt = $2, delivery_error = $3 WHERE id = $1`,
			held.ID, recheck, "destination pending retirement: "+reason); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return retired, err
}

// ClearStale forgets a pending retirement once the destination is found to be the Sandbox it names.
func (s *Store) ClearStale(ctx context.Context, claim Claim) error {
	if claim.staleCheckAt == nil {
		return nil
	}
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		_, err := tx.ExecContext(ctx, `UPDATE inbox SET stale_check_at = NULL WHERE id = $1`, held.ID)
		return err
	})
}

// DueSubscription is a subscription whose source is due to be read.
type DueSubscription struct {
	Subscription
	// ActionsAfterSequence is the sequence of the last event of the Action request this subscription recorded,
	// which is its creation's `after_sequence` until it records one. Nil for a GitHub subscription.
	ActionsAfterSequence *int64
	// GitHubAccess is the access a GitHub subscription was authorized under when it was created, nil for any other.
	GitHubAccess *GitHubAccessKey
	// Generation moves on each time GitHub ingress wakes the subscription, so that a read made before can tell
	// that it may have missed a delivery.
	Generation int64
}

// model.Source returns the claimed inbox's subscription whose source has waited longest past its due time, or nil.
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
	due := DueSubscription{Subscription: subscription, Generation: generation}
	if afterSequence.Valid {
		due.ActionsAfterSequence = &afterSequence.Int64
	}
	if app.Valid {
		due.GitHubAccess = &GitHubAccessKey{AppID: app.Int64, InstallationID: installation.Int64, RepositoryID: repository.Int64}
	}
	return &due, nil
}

// SourceFailed records that the subscription's source could not be processed, and defers its next attempt by
// retryAfter. The failure's start moves only when its kind changes. A subscription that was cancelled, expired
// or renewed since it was read as source (its version moved) keeps its state.
func (s *Store) SourceFailed(ctx context.Context, claim Claim, source Subscription, kind SourceFailureKind, message string, retryAfter time.Duration) error {
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
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
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		result, err := tx.ExecContext(ctx,
			`UPDATE subscription SET next_attempt = $3 WHERE id = $1 AND inbox_id = $2 AND NOT cancelled AND version = $4`,
			source.ID, held.ID, until, source.Version)
		if err != nil {
			return err
		}
		return notifyIfChanged(ctx, tx, result)
	})
}

func notifyIfChanged(ctx context.Context, tx *sql.Tx, result sql.Result) error {
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
	err := s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		var err error
		times, _, err = entriesBeyond(ctx, tx, held.ID, max(held.Covered, held.Acknowledged, held.ExpiredThrough))
		return err
	})
	return times, err
}
