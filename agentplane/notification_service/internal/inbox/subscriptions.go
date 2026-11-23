package inbox

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// Subscription is a subscription as it is read back.
type Subscription struct {
	ID      uuid.UUID
	InboxID uuid.UUID
	// SourceProvider is the stored provider discriminator used to route source work.
	SourceProvider string
	Source         sourceadapter.Source
	IdempotencyKey string
	// Version changes whenever the subscription does.
	Version       int64
	Cancelled     bool
	ExpiresAt     time.Time
	LastSuccessAt *time.Time
	Failure       *model.SourceFailure
	// RetryAt is when the service will retry the current failure of an active subscription.
	RetryAt *time.Time
	// NextSourceCheckAt is when the source is next reconciled, if scheduled.
	NextSourceCheckAt *time.Time
	// Expired is evaluated at the read's observation time.
	Expired bool
	// SourceStatus is the source-owned shared projection.
	SourceStatus sourceadapter.SubscriptionStatus
}

const subscriptionColumns = `s.id, s.inbox_id, s.creation_proto, s.source_provider,
	s.idempotency_key, s.version, s.cancelled, s.expires_at,
	s.last_success_at, s.error, s.error_kind, s.error_since, s.error_observed_at, s.next_attempt`

// scanSubscription reads subscriptionColumns, then any columns the query adds into extra. viewedAt is the time
// the subscription is judged expired at.
func scanSubscription(row rowScanner, viewedAt time.Time, extra ...any) (Subscription, error) {
	var subscription Subscription
	var creationProto []byte
	var sourceProvider string
	var message, kind sql.NullString
	var lastSuccess, since, observed, nextAttempt sql.NullTime
	if err := row.Scan(append([]any{
		&subscription.ID, &subscription.InboxID, &creationProto, &sourceProvider, &subscription.IdempotencyKey,
		&subscription.Version, &subscription.Cancelled, &subscription.ExpiresAt, &lastSuccess, &message, &kind,
		&since, &observed, &nextAttempt,
	}, extra...)...); err != nil {
		return Subscription{}, err
	}
	body, err := sourceadapter.DecodeStoredSubscriptionCreation(subscription.ID, creationProto, sourceProvider, subscription.IdempotencyKey)
	if err != nil {
		return Subscription{}, err
	}
	subscription.Source = body.Source
	subscription.SourceProvider = sourceProvider
	subscription.ExpiresAt = subscription.ExpiresAt.UTC()
	subscription.Expired = !subscription.ExpiresAt.After(viewedAt)
	if nextAttempt.Valid {
		subscription.NextSourceCheckAt = utc(nextAttempt.Time)
	}
	if lastSuccess.Valid {
		subscription.LastSuccessAt = utc(lastSuccess.Time)
	}
	if message.Valid {
		parsed, err := model.ParseSourceFailureKind(kind.String)
		if err != nil {
			return Subscription{}, fmt.Errorf("subscription %s: %w", subscription.ID, err)
		}
		subscription.Failure = &model.SourceFailure{
			Kind: parsed, Message: message.String, Since: since.Time.UTC(), ObservedAt: observed.Time.UTC(),
		}
		if nextAttempt.Valid && !subscription.Cancelled && subscription.ExpiresAt.After(viewedAt) {
			subscription.RetryAt = utc(nextAttempt.Time)
		}
	}
	return subscription, nil
}

func utc(t time.Time) *time.Time {
	t = t.UTC()
	return &t
}

func getSubscription(ctx context.Context, tx transaction.Tx, id uuid.UUID, viewedAt time.Time) (Subscription, error) {
	subscription, err := scanSubscription(tx.QueryRowContext(ctx,
		`SELECT `+subscriptionColumns+` FROM subscription s WHERE s.id = $1`, id), viewedAt)
	if err != nil {
		return Subscription{}, err
	}
	subscription.SourceStatus, err = sourceadapter.LoadSubscriptionStatus(ctx, tx, subscription.ID, subscription.Source)
	return subscription, err
}

func expiry(from time.Time, lifetimeDays int64) time.Time {
	return from.Add(time.Duration(lifetimeDays) * 24 * time.Hour)
}

// checkActiveSubscriptionQuota checks capacity for one more active subscription of the inbox, which the caller
// holds locked, on creation and on the renewal of an expired one.
func (s *Store) checkActiveSubscriptionQuota(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, at time.Time) error {
	var count int
	if err := tx.QueryRowContext(ctx,
		`SELECT count(*) FROM subscription WHERE inbox_id = $1 AND NOT cancelled AND expires_at > $2`,
		inboxID, at).Scan(&count); err != nil {
		return err
	}
	if count >= s.quotas.ActiveSubscriptionsPerInbox {
		return &QuotaError{Quota: ActiveSubscriptionsPerInbox, Limit: s.quotas.ActiveSubscriptionsPerInbox}
	}
	return nil
}

// Subscribe creates the owner's subscription, and the Session's inbox with the first one. A creation whose
// idempotency key the inbox already holds returns that subscription, cancelled or not, if it was created with
// the same arguments, and fails with ErrIdempotencyKeyReused if not.
//
// Authorization records the successful source authorization. A replay returns the subscription it made and writes no rows.
func (s *Store) Subscribe(ctx context.Context, owner Owner, body sourceadapter.Subscribe, authorization sourceadapter.Authorization) (Subscription, error) {
	if err := body.Validate(); err != nil {
		return Subscription{}, err
	}
	if err := sourceadapter.CheckAuthorization(body.Source, authorization); err != nil {
		return Subscription{}, err
	}
	creationProto, sourceProvider, err := sourceadapter.EncodeSubscriptionCreation(body)
	if err != nil {
		return Subscription{}, err
	}
	var subscription Subscription
	err = s.transaction(ctx, func(tx transaction.Tx) error {
		if err := sourceadapter.LockSubscriptionTx(ctx, tx, body.Source, authorization); err != nil {
			return err
		}
		inbox, err := s.ensureInbox(ctx, tx, owner, body.SessionID)
		if err != nil {
			return err
		}
		written := now()
		var existingID uuid.UUID
		var existingProto []byte
		var existingProvider string
		err = tx.QueryRowContext(ctx, `SELECT id, creation_proto, source_provider
			FROM subscription WHERE inbox_id = $1 AND idempotency_key = $2`,
			inbox.ID, body.IdempotencyKey).Scan(&existingID, &existingProto, &existingProvider)
		if err == nil {
			previous, err := sourceadapter.DecodeStoredSubscriptionCreation(existingID, existingProto, existingProvider, body.IdempotencyKey)
			if err != nil {
				return err
			}
			if !previous.Equal(body) {
				return ErrIdempotencyKeyReused
			}
			subscription, err = getSubscription(ctx, tx, existingID, written)
			return err
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err := s.checkActiveSubscriptionQuota(ctx, tx, inbox.ID, written); err != nil {
			return err
		}
		binding, err := sourceadapter.BindSubscriptionTx(ctx, tx, body.Source, authorization)
		if err != nil {
			return err
		}
		id := uuid.New()
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO subscription (id, inbox_id, idempotency_key, creation_proto, source_provider,
				version, actions_after_sequence,
				github_start_position, github_app_id, github_installation_id, github_repository_id, github_subject_kind,
				github_subject_key, github_commit_after_sequence, github_pr_state_after_sequence, generation, cancelled, expires_at, next_attempt)
			VALUES ($1, $2, $3, $4, $5, 1, $6, $7, $8, $9, $10, $11, $12, coalesce($13, 0), coalesce($14, 0), 0, false, $15, $16)`,
			id, inbox.ID, body.IdempotencyKey, creationProto, sourceProvider, binding.ActionsAfterSequence,
			binding.GitHubStartPosition, binding.GitHubApp, binding.GitHubInstallation, binding.GitHubRepository,
			binding.GitHubSubjectKind, binding.GitHubSubjectKey, binding.GitHubCommitAfterSequence,
			binding.GitHubPRStateAfterSequence, expiry(written, body.LifetimeDays), written); err != nil {
			return err
		}
		if err := sourceadapter.InitializeSubscriptionTx(ctx, tx, id, body.Source, authorization); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET updated_at = $2, next_attempt = $2 WHERE id = $1`,
			inbox.ID, written); err != nil {
			return err
		}
		if err := notify(ctx, tx); err != nil {
			return err
		}
		subscription, err = getSubscription(ctx, tx, id, written)
		return err
	})
	return subscription, err
}

// ListSubscriptions returns the owner's subscriptions in ID order after afterID, at most 128.
func (s *Store) ListSubscriptions(ctx context.Context, owner Owner, afterID *uuid.UUID) ([]Subscription, error) {
	viewedAt := now()
	predicate, args := ownerPredicate(owner, "i.", 2)
	rows, err := s.db.QueryContext(ctx, `
		SELECT `+subscriptionColumns+` FROM subscription s JOIN inbox i ON i.id = s.inbox_id
		WHERE s.id > $1 AND `+predicate+`
		ORDER BY s.id LIMIT 128`, append([]any{startAfter(afterID)}, args...)...)
	if err != nil {
		return nil, err
	}
	return readSubscriptions(ctx, s.db, rows, viewedAt)
}

// querier is the read surface a database and a transaction share.
type querier interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// readSubscriptions reads rows of subscriptionColumns to the end and closes them, then loads each subscription's
// source status through q. The rows are closed first because a transaction has one connection.
func readSubscriptions(ctx context.Context, q querier, rows *sql.Rows, viewedAt time.Time) ([]Subscription, error) {
	defer rows.Close()
	var subscriptions []Subscription
	for rows.Next() {
		subscription, err := scanSubscription(rows, viewedAt)
		if err != nil {
			return nil, err
		}
		subscriptions = append(subscriptions, subscription)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	for i, subscription := range subscriptions {
		sourceStatus, err := sourceadapter.LoadSubscriptionStatus(ctx, q, subscription.ID, subscription.Source)
		if err != nil {
			return nil, err
		}
		subscriptions[i].SourceStatus = sourceStatus
	}
	return subscriptions, nil
}

// GetSubscription returns one of the owner's subscriptions.
func (s *Store) GetSubscription(ctx context.Context, owner Owner, id uuid.UUID) (Subscription, error) {
	predicate, args := ownerPredicate(owner, "i.", 2)
	subscription, err := scanSubscription(s.db.QueryRowContext(ctx, `
		SELECT `+subscriptionColumns+` FROM subscription s JOIN inbox i ON i.id = s.inbox_id
		WHERE s.id = $1 AND `+predicate, append([]any{id}, args...)...), now())
	if errors.Is(err, sql.ErrNoRows) {
		return Subscription{}, ErrNotFound
	}
	if err != nil {
		return Subscription{}, err
	}
	subscription.SourceStatus, err = sourceadapter.LoadSubscriptionStatus(ctx, s.db, subscription.ID, subscription.Source)
	return subscription, err
}

// lockSubscription is the owner's subscription read once its inbox is locked for update, so that nothing else
// changes either while the caller does. A retired inbox fails with ErrInboxRetired.
func lockSubscription(ctx context.Context, tx transaction.Tx, owner Owner, id uuid.UUID, viewedAt time.Time) (Subscription, error) {
	var inboxID uuid.UUID
	err := tx.QueryRowContext(ctx, `SELECT inbox_id FROM subscription WHERE id = $1`, id).Scan(&inboxID)
	if errors.Is(err, sql.ErrNoRows) {
		return Subscription{}, ErrNotFound
	}
	if err != nil {
		return Subscription{}, err
	}
	inbox, err := owned(ctx, tx, inboxID, owner)
	if err != nil {
		return Subscription{}, err
	}
	if inbox.Retired {
		return Subscription{}, ErrInboxRetired
	}
	return getSubscription(ctx, tx, id, viewedAt)
}

// Renew extends a subscription to lifetimeDays from now. version must be the one last read: a changed version
// or a cancelled subscription fails with ErrVersionChanged. Renewing an expired subscription takes an active
// slot, subject to the same quota as creating one.
func (s *Store) Renew(ctx context.Context, owner Owner, id uuid.UUID, version, lifetimeDays int64) (Subscription, error) {
	if version < 1 {
		return Subscription{}, model.Invalid("version must be at least 1")
	}
	if lifetimeDays < 1 || lifetimeDays > 30 {
		return Subscription{}, model.Invalid("lifetime_days must be 1 to 30")
	}
	var renewed Subscription
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		written := now()
		current, err := lockSubscription(ctx, tx, owner, id, written)
		if err != nil {
			return err
		}
		if current.Version != version || current.Cancelled {
			return ErrVersionChanged
		}
		if !current.ExpiresAt.After(written) {
			if err := s.checkActiveSubscriptionQuota(ctx, tx, current.InboxID, written); err != nil {
				return err
			}
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE subscription SET expires_at = $2, next_attempt = $3, version = version + 1 WHERE id = $1`,
			id, expiry(written, lifetimeDays), written); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET next_attempt = $2 WHERE id = $1`, current.InboxID, written); err != nil {
			return err
		}
		if err := notify(ctx, tx); err != nil {
			return err
		}
		renewed, err = getSubscription(ctx, tx, id, written)
		return err
	})
	return renewed, err
}

// Cancel cancels a subscription. Cancelling a cancelled one returns it unchanged. Entries it already delivered
// stay in the inbox.
func (s *Store) Cancel(ctx context.Context, owner Owner, id uuid.UUID) (Subscription, error) {
	var cancelled Subscription
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		written := now()
		current, err := lockSubscription(ctx, tx, owner, id, written)
		if err != nil {
			return err
		}
		if current.Cancelled {
			cancelled = current
			return nil
		}
		if _, err := tx.ExecContext(ctx, `UPDATE subscription SET cancelled = true, version = version + 1 WHERE id = $1`, id); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET next_attempt = $2 WHERE id = $1`, current.InboxID, written); err != nil {
			return err
		}
		if err := notify(ctx, tx); err != nil {
			return err
		}
		cancelled, err = getSubscription(ctx, tx, id, written)
		return err
	})
	return cancelled, err
}
