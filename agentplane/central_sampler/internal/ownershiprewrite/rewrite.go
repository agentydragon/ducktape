// Package ownershiprewrite provides the explicit, manifest-scoped Session owner cutover.
package ownershiprewrite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/lib/pq"
)

type owner struct {
	accountUID      string
	serviceSPIFFEID sql.NullString
}

// Forward changes exactly the listed Sessions from accountUID ownership to the Sessions service.
// The manifest must be the complete current account-owned Session set. Repeating a completed
// rewrite is safe when the account has no remaining Sessions and every manifest entry is owned by
// the Sessions service.
func Forward(ctx context.Context, db *sql.DB, sessionIDs []string, accountUID string) error {
	if err := validateInput(sessionIDs, accountUID); err != nil {
		return err
	}

	return withLockedSessionTable(ctx, db, func(tx *sql.Tx) error {
		accountIDs, err := accountSessionIDs(ctx, tx, accountUID)
		if err != nil {
			return err
		}

		if sameIDs(accountIDs, sessionIDs) {
			owners, err := ownersForIDs(ctx, tx, sessionIDs)
			if err != nil {
				return err
			}
			for _, id := range sessionIDs {
				current := owners[id]
				if current.accountUID != accountUID || current.serviceSPIFFEID.Valid {
					return fmt.Errorf("Session ownership changed during forward cutover")
				}
			}

			result, err := tx.ExecContext(ctx, `
				UPDATE sampler_session
				SET account_uid = '', service_spiffe_id = $1
				WHERE account_uid = $2 AND id = ANY($3::text[])`,
				peertls.SessionsURI, accountUID, pq.Array(sessionIDs))
			if err != nil {
				return fmt.Errorf("assign Sessions service ownership: %w", err)
			}
			if err := requireRowsAffected(result, len(sessionIDs)); err != nil {
				return err
			}
			return nil
		}

		// A retry after a successful forward operation is a no-op, but only when the account has
		// no remaining Sessions and the saved manifest is still wholly owned by this service.
		if len(accountIDs) == 0 {
			owners, err := ownersForIDs(ctx, tx, sessionIDs)
			if err != nil {
				return err
			}
			if allOwnedByService(owners, sessionIDs) {
				return nil
			}
		}

		return fmt.Errorf("Session-ID manifest does not exactly match all Sessions currently owned by the supplied account UID")
	})
}

// Reverse restores only the listed Sessions to accountUID. Each entry must either still be owned
// by the Sessions service or already have the requested account owner, making retries idempotent.
func Reverse(ctx context.Context, db *sql.DB, sessionIDs []string, accountUID string) error {
	if err := validateInput(sessionIDs, accountUID); err != nil {
		return err
	}

	return withLockedSessionTable(ctx, db, func(tx *sql.Tx) error {
		owners, err := ownersForIDs(ctx, tx, sessionIDs)
		if err != nil {
			return err
		}

		serviceOwned := make([]string, 0, len(sessionIDs))
		for _, id := range sessionIDs {
			current, ok := owners[id]
			if !ok {
				return fmt.Errorf("Session-ID manifest contains an unknown Session")
			}
			switch {
			case current.accountUID == "" && current.serviceSPIFFEID.Valid && current.serviceSPIFFEID.String == peertls.SessionsURI:
				serviceOwned = append(serviceOwned, id)
			case current.accountUID == accountUID && !current.serviceSPIFFEID.Valid:
				// Already restored by an earlier invocation.
			default:
				return fmt.Errorf("Session-ID manifest contains a Session not owned by the Sessions service or supplied account UID")
			}
		}

		if len(serviceOwned) == 0 {
			return nil
		}
		result, err := tx.ExecContext(ctx, `
			UPDATE sampler_session
			SET account_uid = $1, service_spiffe_id = NULL
			WHERE account_uid = '' AND service_spiffe_id = $2 AND id = ANY($3::text[])`,
			accountUID, peertls.SessionsURI, pq.Array(serviceOwned))
		if err != nil {
			return fmt.Errorf("restore account ownership: %w", err)
		}
		return requireRowsAffected(result, len(serviceOwned))
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

func withLockedSessionTable(ctx context.Context, db *sql.DB, operation func(*sql.Tx) error) error {
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
	if err := operation(tx); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit ownership cutover: %w", err)
	}
	return nil
}

func accountSessionIDs(ctx context.Context, tx *sql.Tx, accountUID string) ([]string, error) {
	rows, err := tx.QueryContext(ctx, `SELECT id FROM sampler_session WHERE account_uid = $1 ORDER BY id`, accountUID)
	if err != nil {
		return nil, fmt.Errorf("list Sessions owned by account UID: %w", err)
	}
	defer rows.Close()

	var ids []string
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

func ownersForIDs(ctx context.Context, tx *sql.Tx, sessionIDs []string) (map[string]owner, error) {
	rows, err := tx.QueryContext(ctx, `
		SELECT id, account_uid, service_spiffe_id
		FROM sampler_session
		WHERE id = ANY($1::text[])`, pq.Array(sessionIDs))
	if err != nil {
		return nil, fmt.Errorf("read Session ownership: %w", err)
	}
	defer rows.Close()

	owners := make(map[string]owner, len(sessionIDs))
	for rows.Next() {
		var id string
		var current owner
		if err := rows.Scan(&id, &current.accountUID, &current.serviceSPIFFEID); err != nil {
			return nil, fmt.Errorf("read Session owner: %w", err)
		}
		owners[id] = current
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read Session owners: %w", err)
	}
	return owners, nil
}

func allOwnedByService(owners map[string]owner, sessionIDs []string) bool {
	if len(owners) != len(sessionIDs) {
		return false
	}
	for _, id := range sessionIDs {
		current, ok := owners[id]
		if !ok || current.accountUID != "" || !current.serviceSPIFFEID.Valid || current.serviceSPIFFEID.String != peertls.SessionsURI {
			return false
		}
	}
	return true
}

func sameIDs(actual, expected []string) bool {
	if len(actual) != len(expected) {
		return false
	}
	actualCopy := append([]string(nil), actual...)
	expectedCopy := append([]string(nil), expected...)
	sort.Strings(actualCopy)
	sort.Strings(expectedCopy)
	for i := range actualCopy {
		if actualCopy[i] != expectedCopy[i] {
			return false
		}
	}
	return true
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
