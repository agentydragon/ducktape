// Package projection keeps each Session's materialized Thread rows current with its stored history.
package projection

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// ErrLeaseLost means this replica no longer owns authority to commit projections for the Session.
var ErrLeaseLost = errors.New("projection lease lost")

// Lease is one replica's ownership of projection work for a Session.
type Lease struct {
	SessionID uuid.UUID
	Token     uuid.UUID
}

func positive(duration time.Duration) error {
	if duration <= 0 {
		return errors.New("projection lease duration must be positive")
	}
	return nil
}

// AcquireLease takes the Session's lease unless another replica holds it unexpired; it returns nil then.
func AcquireLease(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, duration time.Duration) (*Lease, error) {
	if err := positive(duration); err != nil {
		return nil, err
	}
	token := uuid.New()
	var acquired uuid.UUID
	err := tx.QueryRowContext(ctx, `INSERT INTO session_projection_lease (session_id, token, expires_at)
		VALUES ($1, $2, clock_timestamp() + make_interval(secs => $3))
		ON CONFLICT (session_id) DO UPDATE SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
		WHERE session_projection_lease.expires_at <= clock_timestamp()
		RETURNING token`, sessionID, token, duration.Seconds()).Scan(&acquired)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &Lease{SessionID: sessionID, Token: acquired}, nil
}

// RenewLease extends a lease still held and unexpired, and reports whether it did.
func RenewLease(ctx context.Context, tx *sql.Tx, lease Lease, duration time.Duration) (bool, error) {
	if err := positive(duration); err != nil {
		return false, err
	}
	result, err := tx.ExecContext(ctx, `UPDATE session_projection_lease
		SET expires_at = clock_timestamp() + make_interval(secs => $3)
		WHERE session_id = $1 AND token = $2 AND expires_at > clock_timestamp()`,
		lease.SessionID, lease.Token, duration.Seconds())
	if err != nil {
		return false, err
	}
	renewed, err := result.RowsAffected()
	return renewed == 1, err
}

// ReleaseLease hands a lease back so another replica need not wait out its expiry.
func ReleaseLease(ctx context.Context, tx *sql.Tx, lease Lease) error {
	_, err := tx.ExecContext(ctx, "DELETE FROM session_projection_lease WHERE session_id = $1 AND token = $2",
		lease.SessionID, lease.Token)
	return err
}

// FenceLease fails with ErrLeaseLost unless lease owns threadID and is unexpired, and holds its row lock to
// the end of tx, so a takeover or renewal waits until the fenced writes commit or roll back.
func FenceLease(ctx context.Context, tx *sql.Tx, lease Lease, threadID uuid.UUID) error {
	var token uuid.UUID
	err := tx.QueryRowContext(ctx, "SELECT token FROM session_projection_lease WHERE session_id = $1 FOR UPDATE",
		lease.SessionID).Scan(&token)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("%w: no lease for %s", ErrLeaseLost, lease.SessionID)
	}
	if err != nil {
		return err
	}
	// Read the clock only after the lock: a transaction that waited on an owner must not rely on its
	// transaction-start timestamp.
	var expired bool
	if err := tx.QueryRowContext(ctx, "SELECT expires_at <= clock_timestamp() FROM session_projection_lease WHERE session_id = $1",
		lease.SessionID).Scan(&expired); err != nil {
		return err
	}
	if token != lease.Token || expired {
		return fmt.Errorf("%w: %s", ErrLeaseLost, lease.SessionID)
	}
	if threadID != lease.SessionID {
		return fmt.Errorf("%w: lease for %s does not own %s", ErrLeaseLost, lease.SessionID, threadID)
	}
	return nil
}
