package projection

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/lease"
	"github.com/google/uuid"
)

// The lease contract (expiry, takeover, stale fences) is //agentplane/postgres/go/lease's; these cover how the
// projection maps it.

const leaseDuration = time.Minute

func inTx(db *sql.DB, f func(tx *sql.Tx) error) error {
	tx, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		return err
	}
	if err := f(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

func acquire(t *testing.T, db *sql.DB, sessionID uuid.UUID) *Lease {
	t.Helper()
	var held *Lease
	if err := inTx(db, func(tx *sql.Tx) (err error) {
		held, err = AcquireLease(context.Background(), tx, sessionID, leaseDuration)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	return held
}

func renew(t *testing.T, db *sql.DB, held Lease) bool {
	t.Helper()
	var renewed bool
	if err := inTx(db, func(tx *sql.Tx) (err error) {
		renewed, err = RenewLease(context.Background(), tx, held, leaseDuration)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	return renewed
}

func fence(db *sql.DB, held Lease, threadID uuid.UUID) error {
	return inTx(db, func(tx *sql.Tx) error { return FenceLease(context.Background(), tx, held, threadID) })
}

func TestLeaseOwnsOnlyItsSession(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	held := acquire(t, db, sessionID)
	if held == nil {
		t.Fatal("no lease")
	}
	if acquire(t, db, sessionID) != nil {
		t.Fatal("a held lease was acquired again")
	}
	if err := fence(db, *held, sessionID); err != nil {
		t.Fatal(err)
	}
	if err := fence(db, *held, uuid.New()); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("fence for another Session = %v, want lease.ErrLost", err)
	}
}

func TestLostLeaseIsNotRenewed(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	held := acquire(t, db, sessionID)
	if !renew(t, db, *held) {
		t.Fatal("the holder could not renew")
	}
	if err := inTx(db, func(tx *sql.Tx) error { return ReleaseLease(context.Background(), tx, *held) }); err != nil {
		t.Fatal(err)
	}
	if renew(t, db, *held) {
		t.Fatal("a released lease was renewed")
	}
	if acquire(t, db, sessionID) == nil {
		t.Fatal("a released lease was not acquirable")
	}
}
