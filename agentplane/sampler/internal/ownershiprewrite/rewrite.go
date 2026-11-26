// Package ownershiprewrite provides the explicit, manifest-scoped Session owner cutover.
package ownershiprewrite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/lib/pq"
)

// manifestOwners counts the distinct IDs in the reviewed manifest.
type manifestOwners struct {
	found, account, sessionsService int
}

// Forward changes exactly the listed Sessions from accountUID ownership to the Sessions service.
// The manifest must be the complete current account-owned Session set. Repeating a completed
// rewrite is safe when the account has no remaining Sessions and every manifest entry is owned by
// the Sessions service.
func Forward(ctx context.Context, db *sql.DB, sessionIDs []string, accountUID string) error {
	return withLockedManifest(ctx, db, sessionIDs, accountUID, func(tx *sql.Tx, owners manifestOwners) error {
		var accountSessions int
		if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM sampler_session WHERE account_uid = $1`, accountUID).Scan(&accountSessions); err != nil {
			return fmt.Errorf("count Sessions owned by account UID: %w", err)
		}
		if accountSessions == 0 && owners.sessionsService == len(sessionIDs) {
			return nil // The forward rewrite already committed.
		}
		if accountSessions != len(sessionIDs) || owners.account != len(sessionIDs) {
			return errors.New("Session-ID manifest does not exactly match all Sessions currently owned by the supplied account UID")
		}
		result, err := tx.ExecContext(ctx, `
			UPDATE sampler_session
			SET account_uid = '', service_spiffe_id = $1
			WHERE account_uid = $2 AND id = ANY($3::text[])`,
			peertls.SessionsURI, accountUID, pq.Array(sessionIDs))
		if err != nil {
			return fmt.Errorf("assign Sessions service ownership: %w", err)
		}
		return requireRowsAffected(result, len(sessionIDs))
	})
}

// Reverse restores only the listed Sessions to accountUID. Each entry must either still be owned
// by the Sessions service or already have the requested account owner, making retries idempotent.
func Reverse(ctx context.Context, db *sql.DB, sessionIDs []string, accountUID string) error {
	return withLockedManifest(ctx, db, sessionIDs, accountUID, func(tx *sql.Tx, owners manifestOwners) error {
		if owners.found != len(sessionIDs) {
			return errors.New("Session-ID manifest contains an unknown Session")
		}
		if owners.account+owners.sessionsService != owners.found {
			return errors.New("Session-ID manifest contains a Session not owned by the Sessions service or supplied account UID")
		}
		if owners.sessionsService == 0 {
			return nil
		}
		result, err := tx.ExecContext(ctx, `
			UPDATE sampler_session
			SET account_uid = $1, service_spiffe_id = NULL
			WHERE service_spiffe_id = $2 AND id = ANY($3::text[])`,
			accountUID, peertls.SessionsURI, pq.Array(sessionIDs))
		if err != nil {
			return fmt.Errorf("restore account ownership: %w", err)
		}
		return requireRowsAffected(result, owners.sessionsService)
	})
}

// Inventory returns the current Session IDs owned by accountUID in stable order. The result is a
// point-in-time manifest; Forward revalidates the complete account-owned set under its table lock
// before changing any ownership, so an intervening write makes the manifest stale instead of
// causing a partial rewrite.
func Inventory(ctx context.Context, db *sql.DB, accountUID string) ([]string, error) {
	if strings.TrimSpace(accountUID) == "" {
		return nil, errors.New("account UID must not be empty")
	}

	rows, err := db.QueryContext(ctx, `SELECT id FROM sampler_session WHERE account_uid = $1 ORDER BY id`, accountUID)
	if err != nil {
		return nil, fmt.Errorf("list Sessions owned by account UID: %w", err)
	}
	defer rows.Close()

	ids := make([]string, 0)
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("read account-owned Session ID: %w", err)
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read account-owned Session IDs: %w", err)
	}
	return ids, nil
}

func validateInput(sessionIDs []string, accountUID string) error {
	if strings.TrimSpace(accountUID) == "" {
		return errors.New("account UID must not be empty")
	}
	if len(sessionIDs) == 0 {
		return errors.New("Session-ID manifest must contain at least one Session ID")
	}
	seen := make(map[string]struct{}, len(sessionIDs))
	for _, id := range sessionIDs {
		if strings.TrimSpace(id) == "" {
			return errors.New("Session-ID manifest contains an empty Session ID")
		}
		if _, exists := seen[id]; exists {
			return errors.New("Session-ID manifest contains duplicate Session IDs")
		}
		seen[id] = struct{}{}
	}
	return nil
}

func withLockedManifest(ctx context.Context, db *sql.DB, sessionIDs []string, accountUID string, operation func(*sql.Tx, manifestOwners) error) error {
	if err := validateInput(sessionIDs, accountUID); err != nil {
		return err
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return fmt.Errorf("begin ownership cutover transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	// Hold off Session inserts and owner changes until validation and the rewrite commit. Under
	// READ COMMITTED, validation sees writes that committed while this lock was being acquired.
	if _, err := tx.ExecContext(ctx, `LOCK TABLE sampler_session IN SHARE ROW EXCLUSIVE MODE`); err != nil {
		return fmt.Errorf("lock sampler_session for ownership cutover: %w", err)
	}
	var owners manifestOwners
	if err := tx.QueryRowContext(ctx, `
		SELECT count(*),
		       count(*) FILTER (WHERE account_uid = $2),
		       count(*) FILTER (WHERE service_spiffe_id = $3)
		FROM sampler_session WHERE id = ANY($1::text[])`,
		pq.Array(sessionIDs), accountUID, peertls.SessionsURI).Scan(&owners.found, &owners.account, &owners.sessionsService); err != nil {
		return fmt.Errorf("read Session ownership: %w", err)
	}
	if err := operation(tx, owners); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit ownership cutover: %w", err)
	}
	return nil
}

func requireRowsAffected(result sql.Result, expected int) error {
	actual, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("check ownership rewrite result: %w", err)
	}
	if actual != int64(expected) {
		return fmt.Errorf("ownership rewrite affected %d Sessions, expected %d", actual, expected)
	}
	return nil
}
