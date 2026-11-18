package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/google/uuid"
)

const (
	LeaseDuration      = 30 * time.Second
	LeaseRenewInterval = 10 * time.Second
)

var sessionDeliveryLease = lease.Table{
	Name: "session_delivery_lease", Key: []string{"session_id"},
	Holder: "holder", Until: "expires_at", Owned: true,
}

// SessionOwner fences a head step, including work before the command is claimed.
type SessionOwner struct {
	SessionID string
	Holder    uuid.UUID
}

func (o SessionOwner) lease() lease.Lease {
	return lease.Lease{Key: []any{o.SessionID}, Holder: o.Holder}
}

// AcquireSessionLease needs only a Session ID, including for Sessions without an Environment.
func (s *Store) AcquireSessionLease(ctx context.Context, sessionID string) (*SessionOwner, error) {
	var acquired lease.Lease
	err := transaction(ctx, s.DB, func(tx *sql.Tx) (err error) {
		acquired, err = sessionDeliveryLease.Acquire(ctx, tx, []any{sessionID}, LeaseDuration)
		return err
	})
	if err != nil {
		return nil, err
	}
	return &SessionOwner{SessionID: sessionID, Holder: acquired.Holder}, nil
}

func ownerMatch(ctx context.Context, tx *sql.Tx, owner SessionOwner) (bool, error) {
	// Cleanup takes the exclusive side before classifying deleted Dispatches. Take
	// the shared side before the lease row, matching admission/cleanup lock order.
	var sandboxUID sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT sandbox_uid FROM session WHERE session_id=$1`, owner.SessionID).Scan(&sandboxUID)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if sandboxUID.Valid && sandboxUID.String != "" {
		if err := lockLiveSandbox(ctx, tx, sandboxUID.String); err != nil {
			if errors.Is(err, ErrSandboxDeleted) {
				return false, nil
			}
			return false, err
		}
	}
	// Admission and Session settlement take this before delivery rows.
	if err := sessionLock(ctx, tx, owner.SessionID); err != nil {
		return false, err
	}
	if err := sessionDeliveryLease.Fence(ctx, tx, owner.lease()); err != nil {
		if errors.Is(err, lease.ErrLost) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func (s *Store) RenewSessionLease(ctx context.Context, owner SessionOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		_, err := sessionDeliveryLease.Renew(ctx, tx, owner.lease(), LeaseDuration)
		return err
	})
}

func (s *Store) ReleaseSessionLease(ctx context.Context, owner SessionOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		return sessionDeliveryLease.Release(ctx, tx, owner.lease())
	})
}

func (s *Store) StillOwns(ctx context.Context, owner SessionOwner) (bool, error) {
	var current bool
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		current, err = ownerMatch(ctx, tx, owner)
		return err
	})
	return current, err
}
