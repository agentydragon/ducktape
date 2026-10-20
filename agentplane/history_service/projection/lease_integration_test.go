package projection

import (
	"context"
	"database/sql"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

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

func tryAcquire(db *sql.DB, sessionID uuid.UUID) (lease *Lease, err error) {
	err = inTx(db, func(tx *sql.Tx) (err error) {
		lease, err = AcquireLease(context.Background(), tx, sessionID, leaseDuration)
		return err
	})
	return lease, err
}

func acquire(t *testing.T, db *sql.DB, sessionID uuid.UUID) *Lease {
	t.Helper()
	lease, err := tryAcquire(db, sessionID)
	if err != nil {
		t.Fatal(err)
	}
	return lease
}

func renew(t *testing.T, db *sql.DB, lease Lease) bool {
	t.Helper()
	var renewed bool
	if err := inTx(db, func(tx *sql.Tx) (err error) {
		renewed, err = RenewLease(context.Background(), tx, lease, leaseDuration)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	return renewed
}

func release(t *testing.T, db *sql.DB, lease Lease) {
	t.Helper()
	if err := inTx(db, func(tx *sql.Tx) error { return ReleaseLease(context.Background(), tx, lease) }); err != nil {
		t.Fatal(err)
	}
}

func fence(db *sql.DB, lease Lease, threadID uuid.UUID) error {
	return inTx(db, func(tx *sql.Tx) error { return FenceLease(context.Background(), tx, lease, threadID) })
}

func expireNow(t *testing.T, db *sql.DB, sessionID uuid.UUID, ago time.Duration) {
	t.Helper()
	if _, err := db.Exec("UPDATE session_projection_lease SET expires_at = clock_timestamp() - make_interval(secs => $2) WHERE session_id = $1",
		sessionID, ago.Seconds()); err != nil {
		t.Fatal(err)
	}
}

func TestFenceRechecksExpiryAfterWaitingForTheLeaseRow(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	lease := acquire(t, db, sessionID)
	if lease == nil {
		t.Fatal("no lease")
	}

	holder, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Rollback()
	if _, err := holder.Exec("SELECT 1 FROM session_projection_lease WHERE session_id = $1 FOR UPDATE", sessionID); err != nil {
		t.Fatal(err)
	}

	fenced := make(chan error, 1)
	fencing := openDB(t, "test-fence")
	go func() { fenced <- fence(fencing, *lease, sessionID) }()

	// The fence queued on the row: it began before the expiry below, so only a clock read after the lock rejects it.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for {
		var waiting int
		if err := db.QueryRowContext(ctx, `SELECT count(*) FROM pg_stat_activity
			WHERE application_name = 'test-fence' AND wait_event_type = 'Lock'`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting > 0 {
			break
		}
		select {
		case err := <-fenced:
			t.Fatalf("fence finished without waiting for the lease row: %v", err)
		case <-ctx.Done():
			t.Fatal("fence never waited on the lease row")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if _, err := holder.Exec("UPDATE session_projection_lease SET expires_at = clock_timestamp() WHERE session_id = $1", sessionID); err != nil {
		t.Fatal(err)
	}
	if err := holder.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-fenced; !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("fence after expiry = %v, want ErrLeaseLost", err)
	}
}

func TestConcurrentReplicasChooseOneOwner(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	var wg sync.WaitGroup
	leases := make([]*Lease, 2)
	errs := make([]error, 2)
	for replica := range leases {
		wg.Add(1)
		go func() {
			defer wg.Done()
			leases[replica], errs[replica] = tryAcquire(db, sessionID)
		}()
	}
	wg.Wait()
	if err := errors.Join(errs...); err != nil {
		t.Fatal(err)
	}
	if (leases[0] == nil) == (leases[1] == nil) {
		t.Fatalf("leases = %v, %v; want exactly one owner", leases[0], leases[1])
	}
}

func TestOnlyTheCurrentLeaseCanFenceOrRenew(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	lease := acquire(t, db, sessionID)
	if lease == nil {
		t.Fatal("no lease")
	}
	if acquire(t, db, sessionID) != nil {
		t.Fatal("a held lease was acquired again")
	}
	if !renew(t, db, *lease) {
		t.Fatal("the holder could not renew")
	}

	expireNow(t, db, sessionID, time.Second)
	if renew(t, db, *lease) {
		t.Fatal("an expired lease was renewed")
	}
	successor := acquire(t, db, sessionID)
	if successor == nil || successor.Token == lease.Token {
		t.Fatalf("successor = %v, want a new token", successor)
	}

	release(t, db, *lease)
	if !renew(t, db, *successor) {
		t.Fatal("releasing the old token cost the successor its lease")
	}
	if err := fence(db, *lease, sessionID); !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("fence by the old token = %v, want ErrLeaseLost", err)
	}
	if err := fence(db, *successor, sessionID); err != nil {
		t.Fatal(err)
	}
	if err := fence(db, *successor, uuid.New()); !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("fence for another Session = %v, want ErrLeaseLost", err)
	}

	release(t, db, *successor)
	if acquire(t, db, sessionID) == nil {
		t.Fatal("a released lease was not acquirable")
	}
}

func TestLeaseDurationMustBePositive(t *testing.T) {
	db := testDB(t)
	if err := inTx(db, func(tx *sql.Tx) error {
		_, err := AcquireLease(context.Background(), tx, uuid.New(), 0)
		return err
	}); err == nil {
		t.Fatal("a zero duration was accepted")
	}
}

func TestFenceWithoutALeaseIsLost(t *testing.T) {
	db := testDB(t)
	sessionID := uuid.New()
	if err := fence(db, Lease{SessionID: sessionID, Token: uuid.New()}, sessionID); !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("fence = %v, want ErrLeaseLost", err)
	}
}
