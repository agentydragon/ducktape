package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// SourceFailureKind is why a source could not be processed.
type SourceFailureKind string

const (
	FailureRateLimited     SourceFailureKind = "rate_limited"
	FailureUnavailable     SourceFailureKind = "unavailable"
	FailureAccessDenied    SourceFailureKind = "access_denied"
	FailureSourceChanged   SourceFailureKind = "source_changed"
	FailureProcessingError SourceFailureKind = "processing_error"
)

func parseFailureKind(kind string) (SourceFailureKind, error) {
	switch parsed := SourceFailureKind(kind); parsed {
	case FailureRateLimited, FailureUnavailable, FailureAccessDenied, FailureSourceChanged, FailureProcessingError:
		return parsed, nil
	default:
		return "", fmt.Errorf("unknown source failure kind %q", kind)
	}
}

// SourceFailure is the current failure of a subscription's processing.
type SourceFailure struct {
	Kind SourceFailureKind
	// Message is a safe description, never an upstream body or a credential.
	Message string
	// Since is when the failure of this kind began.
	Since      time.Time
	ObservedAt time.Time
}

// Subscription is a subscription as it is read back.
type Subscription struct {
	ID             uuid.UUID
	InboxID        uuid.UUID
	Source         Source
	IdempotencyKey string
	// Version changes whenever the subscription does.
	Version       int64
	Cancelled     bool
	ExpiresAt     time.Time
	LastSuccessAt *time.Time
	Failure       *SourceFailure
	// RetryAt is when the service will retry the current failure of an active subscription.
	RetryAt *time.Time
	// GitHub is the shared state behind a GitHub subscription, nil for any other.
	GitHub *GitHubStatus
}

const subscriptionColumns = `s.id, s.inbox_id, s.creation, s.idempotency_key, s.version, s.cancelled, s.expires_at,
	s.last_success_at, s.error, s.error_kind, s.error_since, s.error_observed_at, s.next_attempt`

// scanSubscription reads subscriptionColumns, then any columns the query adds into extra. viewedAt is the time
// the subscription is judged expired at.
func scanSubscription(row rowScanner, viewedAt time.Time, extra ...any) (Subscription, error) {
	var subscription Subscription
	var creation []byte
	var message, kind sql.NullString
	var lastSuccess, since, observed, nextAttempt sql.NullTime
	if err := row.Scan(append([]any{
		&subscription.ID, &subscription.InboxID, &creation, &subscription.IdempotencyKey,
		&subscription.Version, &subscription.Cancelled, &subscription.ExpiresAt, &lastSuccess, &message, &kind,
		&since, &observed, &nextAttempt,
	}, extra...)...); err != nil {
		return Subscription{}, err
	}
	var body Subscribe
	if err := decode(creation, &body); err != nil {
		return Subscription{}, fmt.Errorf("subscription %s creation: %w", subscription.ID, err)
	}
	subscription.Source = body.Source
	subscription.ExpiresAt = subscription.ExpiresAt.UTC()
	if lastSuccess.Valid {
		subscription.LastSuccessAt = utc(lastSuccess.Time)
	}
	if message.Valid {
		parsed, err := parseFailureKind(kind.String)
		if err != nil {
			return Subscription{}, fmt.Errorf("subscription %s: %w", subscription.ID, err)
		}
		subscription.Failure = &SourceFailure{
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

func getSubscription(ctx context.Context, tx *sql.Tx, id uuid.UUID, viewedAt time.Time) (Subscription, error) {
	subscription, err := scanSubscription(tx.QueryRowContext(ctx,
		`SELECT `+subscriptionColumns+` FROM subscription s WHERE s.id = $1`, id), viewedAt)
	if err != nil {
		return Subscription{}, err
	}
	return subscription, loadGitHubStatus(ctx, tx, &subscription)
}

func expiry(from time.Time, lifetimeDays int64) time.Time {
	return from.Add(time.Duration(lifetimeDays) * 24 * time.Hour)
}

// checkActiveSubscriptionQuota checks capacity for one more active subscription of the inbox, which the caller
// holds locked, on creation and on the renewal of an expired one.
func (s *Store) checkActiveSubscriptionQuota(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, at time.Time) error {
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

// sourceBinding is what a subscription's source adds to its row: the columns that tie it to the source's own
// state.
type sourceBinding struct {
	actionsAfterSequence sql.NullInt64
	// githubStartPosition is the newest delivery when a GitHub subscription was created: only later ones are owed.
	githubStartPosition                             sql.NullInt64
	githubCommitAfterSequence                       sql.NullInt64
	githubPRStateAfterSequence                      sql.NullInt64
	githubApp, githubInstallation, githubRepository sql.NullInt64
	githubSubjectKind, githubSubjectKey             sql.NullString
}

// checkAccess requires the access a source needs: a GitHub subscription is bound to the repository access it was
// authorized under, and no other source has any.
func checkAccess(source Source, access *GitHubAccessKey) error {
	_, github := source.(GitHubSource)
	switch {
	case !github && access != nil:
		return errors.New("only a GitHub subscription is bound to GitHub access")
	case github && access == nil:
		return errors.New("a GitHub subscription is bound to the access it was authorized under")
	case github && (access.AppID <= 0 || access.InstallationID <= 0 || access.RepositoryID <= 0):
		return invalid("GitHub access names an App, installation and repository above 0")
	}
	return nil
}

// bindSource is where subscribing meets a source kind. A GitHub subscription also creates, unless they exist, the
// shared rows it reads through: its installation, repository, grant and subject. The caller holds the GitHub lock.
func bindSource(ctx context.Context, tx *sql.Tx, source Source, access *GitHubAccessKey) (sourceBinding, error) {
	switch source := source.(type) {
	case ActionsSource:
		return sourceBinding{actionsAfterSequence: sql.NullInt64{Int64: source.AfterSequence, Valid: true}}, nil
	case GitHubSource:
		subject, err := NewGitHubSubjectKey(access.RepositoryID, source.Subject)
		if err != nil {
			return sourceBinding{}, err
		}
		var start int64
		if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(position), 0) FROM github_delivery`).Scan(&start); err != nil {
			return sourceBinding{}, err
		}
		var commitSequence int64
		if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(sequence), 0) FROM github_commit_event`).Scan(&commitSequence); err != nil {
			return sourceBinding{}, err
		}
		var prStateSequence int64
		if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(sequence), 0) FROM github_pr_state_event`).Scan(&prStateSequence); err != nil {
			return sourceBinding{}, err
		}
		if err := insertGrant(ctx, tx, *access, source.Repository); err != nil {
			return sourceBinding{}, err
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0) ON CONFLICT DO NOTHING`,
			subject.RepositoryID, subject.Kind, subject.Key); err != nil {
			return sourceBinding{}, err
		}
		return sourceBinding{
			githubStartPosition:        sql.NullInt64{Int64: start, Valid: true},
			githubCommitAfterSequence:  sql.NullInt64{Int64: commitSequence, Valid: true},
			githubPRStateAfterSequence: sql.NullInt64{Int64: prStateSequence, Valid: true},
			githubApp:                  sql.NullInt64{Int64: access.AppID, Valid: true},
			githubInstallation:         sql.NullInt64{Int64: access.InstallationID, Valid: true},
			githubRepository:           sql.NullInt64{Int64: access.RepositoryID, Valid: true},
			githubSubjectKind:          sql.NullString{String: subject.Kind, Valid: true},
			githubSubjectKey:           sql.NullString{String: subject.Key, Valid: true},
		}, nil
	default:
		return sourceBinding{}, invalid("source must be an Action request or a GitHub subject")
	}
}

// Subscribe creates the owner's subscription, and the Session's inbox with the first one. A creation whose
// idempotency key the inbox already holds returns that subscription, cancelled or not, if it was created with
// the same arguments, and fails with ErrIdempotencyKeyReused if not.
//
// access is the repository access a GitHub subscription was authorized under, which the caller has just read from
// GitHub, and nil for any other source. A replay of a creation returns the subscription it made and writes no rows.
func (s *Store) Subscribe(ctx context.Context, owner ServiceAccount, body Subscribe, access *GitHubAccessKey) (Subscription, error) {
	if err := body.Validate(); err != nil {
		return Subscription{}, err
	}
	if err := checkAccess(body.Source, access); err != nil {
		return Subscription{}, err
	}
	creation, err := json.Marshal(body)
	if err != nil {
		return Subscription{}, err
	}
	var subscription Subscription
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		if _, github := body.Source.(GitHubSource); github {
			// Lock order: the GitHub lock, then the inbox.
			if err := githubLock(ctx, tx); err != nil {
				return err
			}
		}
		inbox, err := s.ensureInbox(ctx, tx, owner, body.DestinationRef, body.SessionID)
		if err != nil {
			return err
		}
		written := now()
		var existingID uuid.UUID
		var existing []byte
		err = tx.QueryRowContext(ctx, `SELECT id, creation FROM subscription WHERE inbox_id = $1 AND idempotency_key = $2`,
			inbox.ID, body.IdempotencyKey).Scan(&existingID, &existing)
		if err == nil {
			var previous Subscribe
			if err := decode(existing, &previous); err != nil {
				return fmt.Errorf("subscription %s creation: %w", existingID, err)
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
		binding, err := bindSource(ctx, tx, body.Source, access)
		if err != nil {
			return err
		}
		id := uuid.New()
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, actions_after_sequence,
				github_start_position, github_app_id, github_installation_id, github_repository_id, github_subject_kind,
				github_subject_key, github_commit_after_sequence, github_pr_state_after_sequence, generation, cancelled, expires_at, next_attempt)
			VALUES ($1, $2, $3, $4::jsonb, 1, $5, $6, $7, $8, $9, $10, $11, coalesce($12, 0), coalesce($13, 0), 0, false, $14, $15)`,
			id, inbox.ID, body.IdempotencyKey, string(creation), binding.actionsAfterSequence,
			binding.githubStartPosition, binding.githubApp, binding.githubInstallation, binding.githubRepository,
			binding.githubSubjectKind, binding.githubSubjectKey, binding.githubCommitAfterSequence,
			binding.githubPRStateAfterSequence, expiry(written, body.LifetimeDays), written); err != nil {
			return err
		}
		if _, ok := body.Source.(GitHubSource); ok {
			if _, err := tx.ExecContext(ctx, `UPDATE subscription
				SET github_mergeability_after_sequence = (SELECT coalesce(max(sequence), 0) FROM github_mergeability_event)
				WHERE id = $1`, id); err != nil {
				return err
			}
			if source, ok := body.Source.(GitHubSource); ok && binding.githubSubjectKind.String == "pull_request" {
				subject, err := NewGitHubSubjectKey(access.RepositoryID, source.Subject)
				if err != nil {
					return err
				}
				if err := snapshotGitHubCommitObservations(ctx, tx, id, access.AppID, subject, source); err != nil {
					return err
				}
				if err := snapshotGitHubPRState(ctx, tx, id, access.AppID, access.InstallationID, subject, prStateFilters(source)); err != nil {
					return err
				}
			}
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
	rows, err := s.db.QueryContext(ctx, `
		SELECT `+subscriptionColumns+` FROM subscription s JOIN inbox i ON i.id = s.inbox_id
		WHERE i.owner_namespace = $1 AND i.owner_name = $2 AND i.destination_ref ->> 'uid' = $3 AND s.id > $4
		ORDER BY s.id LIMIT 128`, owner.Account.Namespace, owner.Account.Name, owner.SandboxUID, startAfter(afterID))
	if err != nil {
		return nil, err
	}
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
	for i := range subscriptions {
		if err := loadGitHubStatus(ctx, s.db, &subscriptions[i]); err != nil {
			return nil, err
		}
	}
	return subscriptions, nil
}

// GetSubscription returns one of the owner's subscriptions.
func (s *Store) GetSubscription(ctx context.Context, owner Owner, id uuid.UUID) (Subscription, error) {
	subscription, err := scanSubscription(s.db.QueryRowContext(ctx, `
		SELECT `+subscriptionColumns+` FROM subscription s JOIN inbox i ON i.id = s.inbox_id
		WHERE s.id = $1 AND i.owner_namespace = $2 AND i.owner_name = $3 AND i.destination_ref ->> 'uid' = $4`,
		id, owner.Account.Namespace, owner.Account.Name, owner.SandboxUID), now())
	if errors.Is(err, sql.ErrNoRows) {
		return Subscription{}, ErrNotFound
	}
	if err != nil {
		return Subscription{}, err
	}
	return subscription, loadGitHubStatus(ctx, s.db, &subscription)
}

// lockSubscription is the owner's subscription read once its inbox is locked for update, so that nothing else
// changes either while the caller does. A retired inbox fails with ErrInboxRetired.
func lockSubscription(ctx context.Context, tx *sql.Tx, owner Owner, id uuid.UUID, viewedAt time.Time) (Subscription, error) {
	var inboxID uuid.UUID
	err := tx.QueryRowContext(ctx, `SELECT inbox_id FROM subscription WHERE id = $1`, id).Scan(&inboxID)
	if errors.Is(err, sql.ErrNoRows) {
		return Subscription{}, ErrNotFound
	}
	if err != nil {
		return Subscription{}, err
	}
	inbox, err := owned(ctx, tx, inboxID, owner, true)
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
		return Subscription{}, invalid("version must be at least 1")
	}
	if lifetimeDays < 1 || lifetimeDays > 30 {
		return Subscription{}, invalid("lifetime_days must be 1 to 30")
	}
	var renewed Subscription
	err := s.transaction(ctx, func(tx *sql.Tx) error {
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
	err := s.transaction(ctx, func(tx *sql.Tx) error {
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
