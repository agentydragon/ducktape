package lease

import (
	"context"
	"database/sql"
	"errors"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

const duration = time.Minute

// rowTable keeps leases on rows its user owns, ordered by a generation.
var rowTable = Table{Name: "test_row", Key: []string{"id"}, Holder: "holder", Until: "until", Generation: "generation"}

// ownedTable holds nothing but leases, under a composite key.
var ownedTable = Table{Name: "test_owned_lease", Key: []string{"scope", "id"}, Holder: "holder", Until: "until", Owned: true}

// subject is one row of a table under test.
type subject struct {
	table Table
	key   []any
}

// subjects returns a fresh row of each kind of table; a row-attached one already exists, never leased.
func subjects(t *testing.T, db *sql.DB) map[string]subject {
	t.Helper()
	id := uuid.New()
	if _, err := db.Exec("INSERT INTO test_row (id) VALUES ($1)", id); err != nil {
		t.Fatal(err)
	}
	return map[string]subject{
		"row":   {table: rowTable, key: []any{id}},
		"owned": {table: ownedTable, key: []any{"test-scope", uuid.New()}},
	}
}

// openDB opens the fixture database; a non-empty applicationName names its connections in pg_stat_activity.
func openDB(t *testing.T, applicationName string) *sql.DB {
	t.Helper()
	target, err := url.Parse(integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	if applicationName != "" {
		query := target.Query()
		query.Set("application_name", applicationName)
		target.RawQuery = query.Encode()
	}
	db, err := sql.Open("postgres", target.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func inTx(db *sql.DB, f func(tx *sql.Tx) error) error {
	tx, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		return err
	}
	if err := f(tx); err != nil {
		return errors.Join(err, tx.Rollback())
	}
	return tx.Commit()
}

func (s subject) acquire(db *sql.DB) (lease Lease, err error) {
	err = inTx(db, func(tx *sql.Tx) (err error) {
		lease, err = s.table.Acquire(context.Background(), tx, s.key, duration)
		return err
	})
	return lease, err
}

func (s subject) mustAcquire(t *testing.T, db *sql.DB) Lease {
	t.Helper()
	lease, err := s.acquire(db)
	if err != nil {
		t.Fatal(err)
	}
	return lease
}

func (s subject) renew(db *sql.DB, lease Lease) (renewed Lease, err error) {
	err = inTx(db, func(tx *sql.Tx) (err error) {
		renewed, err = s.table.Renew(context.Background(), tx, lease, duration)
		return err
	})
	return renewed, err
}

func (s subject) release(t *testing.T, db *sql.DB, lease Lease) {
	t.Helper()
	if err := inTx(db, func(tx *sql.Tx) error { return s.table.Release(context.Background(), tx, lease) }); err != nil {
		t.Fatal(err)
	}
}

func (s subject) fence(db *sql.DB, lease Lease) error {
	return inTx(db, func(tx *sql.Tx) error { return s.table.Fence(context.Background(), tx, lease) })
}

// expire moves the row's expiry into the past, as time passing would.
func (s subject) expire(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec("UPDATE "+s.table.name()+" SET until = clock_timestamp() - interval '1 second' WHERE "+
		s.table.where(), s.key...); err != nil {
		t.Fatal(err)
	}
}

func TestLiveLeaseRefusesOtherHolders(t *testing.T) {
	db := openDB(t, "")
	for name, s := range subjects(t, db) {
		t.Run(name, func(t *testing.T) {
			lease := s.mustAcquire(t, db)
			_, err := s.acquire(db)
			var held *HeldError
			if !errors.As(err, &held) {
				t.Fatalf("second acquisition = %v, want *HeldError", err)
			}
			if !held.Until.Equal(lease.Until) {
				t.Fatalf("held until %s, want the live lease's expiry %s", held.Until, lease.Until)
			}
			renewed, err := s.renew(db, lease)
			if err != nil {
				t.Fatal(err)
			}
			if renewed.Holder != lease.Holder || renewed.Until.Before(lease.Until) {
				t.Fatalf("renewed = %+v, want the same holder no sooner than %s", renewed, lease.Until)
			}
			if err := s.fence(db, renewed); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestExpiredLeaseIsTakenOverAndItsFenceRejected(t *testing.T) {
	db := openDB(t, "")
	for name, s := range subjects(t, db) {
		t.Run(name, func(t *testing.T) {
			stale := s.mustAcquire(t, db)
			s.expire(t, db)
			if err := s.fence(db, stale); !errors.Is(err, ErrLost) {
				t.Fatalf("fence of an expired lease = %v, want ErrLost", err)
			}
			if _, err := s.renew(db, stale); !errors.Is(err, ErrLost) {
				t.Fatalf("renewal of an expired lease = %v, want ErrLost", err)
			}

			successor := s.mustAcquire(t, db)
			if successor.Holder == stale.Holder {
				t.Fatal("the successor reused the stale holder's fencing token")
			}
			if s.table.Generation != "" && successor.Generation <= stale.Generation {
				t.Fatalf("successor generation %d, want above the stale %d", successor.Generation, stale.Generation)
			}
			if err := s.fence(db, stale); !errors.Is(err, ErrLost) {
				t.Fatalf("stale fence after takeover = %v, want ErrLost", err)
			}
			s.release(t, db, stale)
			if err := s.fence(db, successor); err != nil {
				t.Fatalf("releasing the stale lease cost the successor its lease: %v", err)
			}
		})
	}
}

func TestReleasedLeaseIsAcquirableAtOnce(t *testing.T) {
	db := openDB(t, "")
	for name, s := range subjects(t, db) {
		t.Run(name, func(t *testing.T) {
			lease := s.mustAcquire(t, db)
			s.release(t, db, lease)
			if err := s.fence(db, lease); !errors.Is(err, ErrLost) {
				t.Fatalf("fence of a released lease = %v, want ErrLost", err)
			}
			s.mustAcquire(t, db)
		})
	}
}

func TestConcurrentAcquisitionsChooseOneHolder(t *testing.T) {
	db := openDB(t, "")
	for name, s := range subjects(t, db) {
		t.Run(name, func(t *testing.T) {
			var wg sync.WaitGroup
			errs := make([]error, 2)
			for i := range errs {
				wg.Go(func() { _, errs[i] = s.acquire(db) })
			}
			wg.Wait()
			var held *HeldError
			if (errs[0] == nil) == (errs[1] == nil) || !errors.As(errors.Join(errs...), &held) {
				t.Fatalf("acquisitions = %v; want one lease and one *HeldError", errs)
			}
		})
	}
}

func TestFenceRechecksExpiryAfterWaitingForTheRow(t *testing.T) {
	db := openDB(t, "")
	s := subjects(t, db)["row"]
	lease := s.mustAcquire(t, db)

	holder, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Rollback()
	if _, err := holder.Exec("SELECT 1 FROM test_row WHERE id = $1 FOR UPDATE", s.key...); err != nil {
		t.Fatal(err)
	}

	fenced := make(chan error, 1)
	fencing := openDB(t, "test-fence")
	go func() { fenced <- s.fence(fencing, lease) }()

	// The fence queues on the row before the expiry below, so only a clock read after the lock rejects it.
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
			t.Fatalf("fence finished without waiting for the row: %v", err)
		case <-ctx.Done():
			t.Fatal("fence never waited on the row")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if _, err := holder.Exec("UPDATE test_row SET until = clock_timestamp() WHERE id = $1", s.key...); err != nil {
		t.Fatal(err)
	}
	if err := holder.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-fenced; !errors.Is(err, ErrLost) {
		t.Fatalf("fence after expiry = %v, want ErrLost", err)
	}
}

func TestMissingRow(t *testing.T) {
	db := openDB(t, "")
	s := subject{table: rowTable, key: []any{uuid.New()}}
	if _, err := s.acquire(db); err == nil || errors.As(err, new(*HeldError)) {
		t.Fatalf("acquiring a missing row = %v, want a failure other than *HeldError", err)
	}
	if err := s.fence(db, Lease{Key: s.key, Holder: uuid.New()}); !errors.Is(err, ErrLost) {
		t.Fatalf("fence of a missing row = %v, want ErrLost", err)
	}
}

func TestDurationMustBePositive(t *testing.T) {
	db := openDB(t, "")
	err := inTx(db, func(tx *sql.Tx) error {
		_, err := ownedTable.Acquire(context.Background(), tx, []any{"test-scope", uuid.New()}, 0)
		return err
	})
	if err == nil {
		t.Fatal("a zero duration was accepted")
	}
}
