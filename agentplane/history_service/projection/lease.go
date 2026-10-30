// Package projection keeps each Session's materialized Thread rows current with its stored history.
package projection

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/lease"
	"github.com/google/uuid"
)

var leaseTable = lease.Table{
	Name: "session_projection_lease", Key: []string{"session_id"}, Holder: "token", Until: "expires_at", Owned: true,
}

// Lease is one replica's ownership of projection work for a Session.
type Lease struct {
	SessionID uuid.UUID
	held      lease.Lease
}

// AcquireLease takes the Session's lease unless another replica holds it unexpired; it returns nil then.
func AcquireLease(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, duration time.Duration) (*Lease, error) {
	held, err := leaseTable.Acquire(ctx, tx, []any{sessionID}, duration)
	if errors.As(err, new(*lease.HeldError)) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &Lease{SessionID: sessionID, held: held}, nil
}

// RenewLease extends a lease still held and unexpired, and reports whether it did.
func RenewLease(ctx context.Context, tx *sql.Tx, held Lease, duration time.Duration) (bool, error) {
	_, err := leaseTable.Renew(ctx, tx, held.held, duration)
	if errors.Is(err, lease.ErrLost) {
		return false, nil
	}
	return err == nil, err
}

// ReleaseLease hands a lease back so another replica need not wait out its expiry.
func ReleaseLease(ctx context.Context, tx *sql.Tx, held Lease) error {
	return leaseTable.Release(ctx, tx, held.held)
}

// FenceLease fails with lease.ErrLost unless held owns threadID and is unexpired, and holds its row lock to the
// end of tx, so a takeover or renewal waits until the fenced writes commit or roll back.
func FenceLease(ctx context.Context, tx *sql.Tx, held Lease, threadID uuid.UUID) error {
	if threadID != held.SessionID {
		return fmt.Errorf("%w: lease for %s does not own %s", lease.ErrLost, held.SessionID, threadID)
	}
	return leaseTable.Fence(ctx, tx, held.held)
}
