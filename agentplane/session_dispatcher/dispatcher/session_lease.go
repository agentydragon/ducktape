package dispatcher

import (
	"context"
	"database/sql"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/google/uuid"
)

var sessionDeliveryLease = lease.Table{
	Name: "session_delivery_lease", Key: []string{"session_id"},
	Holder: "holder", Until: "expires_at", Owned: true,
}

// SessionOwner fences a head step, including work before the command is claimed.
type SessionOwner struct {
	// CLEANUP(added 2026-11-16): Remove the Kubernetes Lease/generation proof in PR 2,
	// after the coordinator confirms every staging Sessions replica runs the compatibility image.
	Owner
	SessionID string
	Holder    uuid.UUID
}

func (o SessionOwner) lease() lease.Lease {
	return lease.Lease{Key: []any{o.SessionID}, Holder: o.Holder}
}

// AcquireSessionLease needs only a Session ID, including for Sessions without an Environment.
// The compatibility worker separately requires the Sandbox ownership proof before using it.
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
	current, err := sandboxOwnerMatch(ctx, tx, owner.Owner)
	if err != nil || !current {
		return false, err
	}
	if err := sessionDeliveryLease.Fence(ctx, tx, owner.lease()); err != nil {
		if errors.Is(err, lease.ErrLost) {
			return false, nil
		}
		return false, err
	}
	var matches bool
	err = tx.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM session WHERE session_id=$1 AND sandbox_uid=$2)`, owner.SessionID, owner.SandboxUID).Scan(&matches)
	return matches, err
}

func (s *Store) RenewSessionLease(ctx context.Context, owner SessionOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil {
			return err
		}
		if !current {
			return lease.ErrLost
		}
		_, err = sessionDeliveryLease.Renew(ctx, tx, owner.lease(), LeaseDuration)
		return err
	})
}

func (s *Store) ReleaseSessionLease(ctx context.Context, owner SessionOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		return sessionDeliveryLease.Release(ctx, tx, owner.lease())
	})
}

func (w *DeliveryWorker) stillOwns(ctx context.Context, owner SessionOwner) (bool, error) {
	current, err := w.Leases.StillOwns(ctx, owner.Owner)
	if err != nil || !current {
		return false, err
	}
	err = transaction(ctx, w.Store.DB, func(tx *sql.Tx) error {
		current, err = ownerMatch(ctx, tx, owner)
		return err
	})
	return current, err
}
