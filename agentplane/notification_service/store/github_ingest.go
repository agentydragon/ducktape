package store

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/google/uuid"
)

// ErrDeliveryReused is a delivery ID the App already received with different content.
var ErrDeliveryReused = errors.New("GitHub delivery ID reused with different content")

// GitHubIngest is a validated webhook delivery as it is stored.
type GitHubIngest struct {
	AppID          int64
	DeliveryID     uuid.UUID
	InstallationID int64
	// Repository is nil for an installation event that names none.
	Repository *GitHubRepository
	Event      string
	// Digest identifies the delivery's content: SHA-256 over the event name, a NUL byte and the raw body.
	Digest []byte
	// Payload is the raw body, a JSON object.
	Payload json.RawMessage
	Action  *string
	// HeadSHA is the commit the event is about, if it is about one.
	HeadSHA *string
	// Subjects are the subjects the event refers to, all in Repository.
	Subjects []GitHubSubjectKey
	// Revisions are the commits Subjects are seen at.
	Revisions []GitHubHeadRevision
}

// IngestGitHub stores a delivery and wakes every inbox following the App, and reports whether the delivery was
// new. A redelivery with the same content stores nothing and wakes nothing; with other content it fails with
// ErrDeliveryReused.
//
// An installation or installation_repositories event invalidates what was learned of the installation: its
// generation, and those of its repositories' access rows and subjects, move on, so no result read earlier is
// recorded.
func (s *Store) IngestGitHub(ctx context.Context, ingest GitHubIngest) (bool, error) {
	var created bool
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		// Allocate delivery positions in commit order, which also fences a subscription's start boundary.
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		var digest []byte
		err := tx.QueryRowContext(ctx, `SELECT digest FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`,
			ingest.AppID, ingest.DeliveryID).Scan(&digest)
		if err == nil {
			if !bytes.Equal(digest, ingest.Digest) {
				return ErrDeliveryReused
			}
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err := s.storeDelivery(ctx, tx, ingest); err != nil {
			return err
		}
		if err := wakeGitHubInboxes(ctx, tx, ingest.AppID); err != nil {
			return err
		}
		created = true
		return notify(ctx, tx)
	})
	return created && err == nil, err
}

func (s *Store) storeDelivery(ctx context.Context, tx *sql.Tx, ingest GitHubIngest) error {
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`,
		ingest.AppID, ingest.InstallationID); err != nil {
		return err
	}
	if ingest.Event == "installation" || ingest.Event == "installation_repositories" {
		if err := invalidateInstallation(ctx, tx, ingest.AppID, ingest.InstallationID); err != nil {
			return err
		}
	}
	if ingest.Repository != nil {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
			ingest.Repository.ID, ingest.Repository.Name); err != nil {
			return err
		}
	}
	for _, key := range ingest.Subjects {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0)
			ON CONFLICT DO NOTHING`, key.RepositoryID, key.Kind, key.Key); err != nil {
			return err
		}
		if err := addRevisions(ctx, tx, key, ingest.Revisions); err != nil {
			return err
		}
	}
	if err := invalidateMergeability(ctx, tx, ingest); err != nil {
		return err
	}
	var repositoryID sql.NullInt64
	if ingest.Repository != nil {
		repositoryID = sql.NullInt64{Int64: ingest.Repository.ID, Valid: true}
	}
	var position int64
	if err := tx.QueryRowContext(ctx, `
		INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event, action, head_sha, digest, payload)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) RETURNING position`,
		ingest.AppID, ingest.DeliveryID, ingest.InstallationID, repositoryID, ingest.Event, ingest.Action, ingest.HeadSHA,
		ingest.Digest, string(ingest.Payload)).Scan(&position); err != nil {
		return err
	}
	for _, key := range ingest.Subjects {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO github_delivery_subject (delivery_position, repository_id, kind, subject_key) VALUES ($1, $2, $3, $4)
			ON CONFLICT DO NOTHING`, position, key.RepositoryID, key.Kind, key.Key); err != nil {
			return err
		}
	}
	return nil
}

// invalidateInstallation moves on the generation of an installation and of everything validated or refreshed
// under it, and drops the leases on them, so that a refresh in flight records nothing.
func invalidateInstallation(ctx context.Context, tx *sql.Tx, appID, installationID int64) error {
	if _, err := tx.ExecContext(ctx,
		`UPDATE github_installation SET generation = generation + 1 WHERE app_id = $1 AND installation_id = $2`,
		appID, installationID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE github_subject SET generation = generation + 1, last_success_at = NULL, claim = NULL, claim_until = NULL
		WHERE repository_id IN (
			SELECT repository_id FROM github_repository_access WHERE app_id = $1 AND installation_id = $2)`,
		appID, installationID); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `
		UPDATE github_repository_access
		SET generation = generation + 1, valid_until = NULL, claim = NULL, claim_until = NULL
		WHERE app_id = $1 AND installation_id = $2`, appID, installationID)
	return err
}

// wakeGitHubInboxes makes every inbox with a live subscription to the App due now, and its GitHub subscriptions
// with it. This is conservative: a pull request whose head lives in a fork is woken too.
func wakeGitHubInboxes(ctx context.Context, tx *sql.Tx, appID int64) error {
	// Lock in ID order, after the GitHub lock.
	rows, err := tx.QueryContext(ctx, `
		SELECT id FROM inbox
		WHERE NOT retired AND id IN (
			SELECT inbox_id FROM subscription WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now())
		ORDER BY id FOR UPDATE`, appID)
	if err != nil {
		return err
	}
	var inboxes []string
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		inboxes = append(inboxes, id.String())
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if len(inboxes) == 0 {
		return nil
	}
	ids := "{" + strings.Join(inboxes, ",") + "}"
	at := now()
	// A failure's backoff stands; otherwise the source is due at once.
	if _, err := tx.ExecContext(ctx, `
		UPDATE subscription SET generation = generation + 1,
			next_attempt = COALESCE(CASE WHEN error IS NOT NULL THEN next_attempt END, $3::timestamptz)
		WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now() AND inbox_id = ANY($2::uuid[])`,
		appID, ids, at); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE inbox SET next_attempt = $2 WHERE id = ANY($1::uuid[])`, ids, at)
	return err
}

// CleanupGitHubDeliveries deletes up to 1000 raw receipts and 1000 mergeability transitions older than cutoff.
// Expired transition rows are removed even if no inbox consumed them. Inbox entries and current pull-request
// snapshots have independent lifetimes. The GitHub lock serializes this with ingestion and recording.
func (s *Store) CleanupGitHubDeliveries(ctx context.Context, cutoff time.Time) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			DELETE FROM github_delivery WHERE position IN (
				SELECT position FROM github_delivery WHERE received_at < $1
				ORDER BY received_at, position LIMIT 1000
			)`, cutoff); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `
			DELETE FROM github_mergeability_event WHERE sequence IN (
				SELECT sequence FROM github_mergeability_event WHERE created_at < $1
				ORDER BY created_at, sequence LIMIT 1000
			)`, cutoff)
		return err
	})
}
