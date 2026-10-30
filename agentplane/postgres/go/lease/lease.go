// Package lease grants time-bounded ownership of a PostgreSQL row to one holder at a time.
//
// PostgreSQL's clock_timestamp() is the only clock: a lease is live while its expiry is later than the
// database's clock when it is read, never a replica's. Each acquisition names a fresh holder UUID, the fencing
// token: a write made under a lease runs in a transaction that first calls Fence, which locks the row until the
// transaction ends, so a takeover waits for the write and a write after a takeover fails with ErrLost.
package lease

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

// ErrLost means the lease expired, was taken over, or its row is gone.
var ErrLost = errors.New("lease lost")

// HeldError is an acquisition refused because another holder's lease is live.
type HeldError struct {
	// Until is when the live lease expires unless renewed.
	Until time.Time
}

func (e *HeldError) Error() string { return fmt.Sprintf("lease held until %s", e.Until) }

// Table names the columns a table keeps one lease per row in.
type Table struct {
	Name string
	// Key are the columns identifying a row, in the order of Lease.Key.
	Key []string
	// Holder is the uuid column naming the holder of the current or last lease.
	Holder string
	// Until is the timestamptz column at which the lease expires. NULL is never live.
	Until string
	// Generation, when set, is an integer column each acquisition increments, so holders are ordered.
	Generation string
	// Owned means the table holds nothing but leases: Acquire inserts a missing row and Release deletes it.
	// Otherwise the row belongs to its user and must exist. An owned row cannot keep a Generation.
	Owned bool
}

// Lease is one acquisition of a row.
type Lease struct {
	// Key holds the row's key values, in the order of Table.Key.
	Key []any
	// Holder is the fencing token: it names this acquisition and no other.
	Holder uuid.UUID
	// Generation is the row's generation as this acquisition set it, or 0 if the table keeps none.
	Generation int64
	// Until is when the lease expires, as of its acquisition or last renewal.
	Until time.Time
}

func (t Table) check(key []any) error {
	if t.Owned && t.Generation != "" {
		return fmt.Errorf("lease table %s: an owned row cannot keep a generation", t.Name)
	}
	if len(key) != len(t.Key) {
		return fmt.Errorf("lease table %s: %d key values for %d key columns", t.Name, len(key), len(t.Key))
	}
	return nil
}

func positive(duration time.Duration) error {
	if duration <= 0 {
		return fmt.Errorf("lease duration %s must be positive", duration)
	}
	return nil
}

func (t Table) name() string { return pq.QuoteIdentifier(t.Name) }

func column(name string) string { return pq.QuoteIdentifier(name) }

// where matches the row by key, as placeholders $1 to $len(Key).
func (t Table) where() string {
	terms := make([]string, len(t.Key))
	for i, key := range t.Key {
		terms[i] = fmt.Sprintf("%s = $%d", column(key), i+1)
	}
	return strings.Join(terms, " AND ")
}

// args are the key values followed by extra, numbered after the key's placeholders.
func args(key []any, extra ...any) []any {
	return append(append([]any(nil), key...), extra...)
}

// expiry is the expiry duration from now, as SQL on placeholder n.
func expiry(n int) string {
	return fmt.Sprintf("clock_timestamp() + make_interval(secs => $%d)", n)
}

// Acquire takes the row's lease for duration unless another holder's is live, when it fails with *HeldError.
func (t Table) Acquire(ctx context.Context, tx *sql.Tx, key []any, duration time.Duration) (Lease, error) {
	if err := t.check(key); err != nil {
		return Lease{}, err
	}
	if err := positive(duration); err != nil {
		return Lease{}, err
	}
	holder, until := column(t.Holder), column(t.Until)
	n := len(key)
	lease := Lease{Key: key, Holder: uuid.New()}
	values := args(key, lease.Holder, duration.Seconds())
	var query string
	if t.Owned {
		columns := make([]string, n)
		placeholders := make([]string, n)
		for i, key := range t.Key {
			columns[i], placeholders[i] = column(key), fmt.Sprintf("$%d", i+1)
		}
		query = fmt.Sprintf(`INSERT INTO %[1]s (%[2]s, %[3]s, %[4]s) VALUES (%[5]s, $%[6]d, %[7]s)
			ON CONFLICT (%[2]s) DO UPDATE SET %[3]s = EXCLUDED.%[3]s, %[4]s = EXCLUDED.%[4]s
			WHERE %[1]s.%[4]s <= clock_timestamp()
			RETURNING %[4]s, 0`,
			t.name(), strings.Join(columns, ", "), holder, until, strings.Join(placeholders, ", "), n+1, expiry(n+2))
	} else {
		// A clock read before this statement waits on the row's lock is earlier than the true time, so it can only
		// refuse a lease that expired during the wait, never take a live one.
		generation := "0"
		set := fmt.Sprintf("%s = $%d, %s = %s", holder, n+1, until, expiry(n+2))
		if t.Generation != "" {
			generation = column(t.Generation)
			set += fmt.Sprintf(", %[1]s = %[1]s + 1", generation)
		}
		query = fmt.Sprintf(`UPDATE %s SET %s WHERE %s AND (%s IS NULL OR %s <= clock_timestamp()) RETURNING %s, %s`,
			t.name(), set, t.where(), until, until, until, generation)
	}
	err := tx.QueryRowContext(ctx, query, values...).Scan(&lease.Until, &lease.Generation)
	if errors.Is(err, sql.ErrNoRows) {
		var held time.Time
		if err := tx.QueryRowContext(ctx, fmt.Sprintf("SELECT %s FROM %s WHERE %s", until, t.name(), t.where()), key...).
			Scan(&held); err != nil {
			return Lease{}, fmt.Errorf("lease table %s row %v: %w", t.Name, key, err)
		}
		return Lease{}, &HeldError{Until: held}
	}
	if err != nil {
		return Lease{}, err
	}
	return lease, nil
}

// Fence fails with ErrLost unless lease is the row's live lease, and otherwise holds the row locked to the end
// of tx, so a renewal, release or takeover waits until the writes fenced by it commit or roll back.
func (t Table) Fence(ctx context.Context, tx *sql.Tx, lease Lease) error {
	if err := t.check(lease.Key); err != nil {
		return err
	}
	generation := "0"
	if t.Generation != "" {
		generation = column(t.Generation)
	}
	var holder uuid.NullUUID
	var current int64
	err := tx.QueryRowContext(ctx, fmt.Sprintf("SELECT %s, %s FROM %s WHERE %s FOR UPDATE",
		column(t.Holder), generation, t.name(), t.where()), lease.Key...).Scan(&holder, &current)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("%w: no row %s %v", ErrLost, t.Name, lease.Key)
	}
	if err != nil {
		return err
	}
	// Read the clock only once the row is locked: a statement that waited on the lock evaluated its clock
	// before the wait.
	var live bool
	if err := tx.QueryRowContext(ctx, fmt.Sprintf("SELECT COALESCE(%s > clock_timestamp(), FALSE) FROM %s WHERE %s",
		column(t.Until), t.name(), t.where()), lease.Key...).Scan(&live); err != nil {
		return err
	}
	if !holder.Valid || holder.UUID != lease.Holder || current != lease.Generation || !live {
		return fmt.Errorf("%w: %s %v", ErrLost, t.Name, lease.Key)
	}
	return nil
}

// Renew extends a live lease to duration from now, or fails with ErrLost. The row stays locked to the end of tx.
func (t Table) Renew(ctx context.Context, tx *sql.Tx, lease Lease, duration time.Duration) (Lease, error) {
	if err := positive(duration); err != nil {
		return Lease{}, err
	}
	if err := t.Fence(ctx, tx, lease); err != nil {
		return Lease{}, err
	}
	n := len(lease.Key)
	if err := tx.QueryRowContext(ctx, fmt.Sprintf("UPDATE %s SET %s = %s WHERE %s RETURNING %s",
		t.name(), column(t.Until), expiry(n+1), t.where(), column(t.Until)),
		args(lease.Key, duration.Seconds())...).Scan(&lease.Until); err != nil {
		return Lease{}, err
	}
	return lease, nil
}

// Release ends the lease now, if it is still this holder's, so the next holder need not wait out its expiry.
// Releasing a lost lease does nothing.
func (t Table) Release(ctx context.Context, tx *sql.Tx, lease Lease) error {
	if err := t.check(lease.Key); err != nil {
		return err
	}
	n := len(lease.Key)
	query := fmt.Sprintf("UPDATE %[1]s SET %[2]s = LEAST(%[2]s, clock_timestamp()) WHERE %[3]s AND %[4]s = $%[5]d",
		t.name(), column(t.Until), t.where(), column(t.Holder), n+1)
	if t.Owned {
		query = fmt.Sprintf("DELETE FROM %s WHERE %s AND %s = $%d", t.name(), t.where(), column(t.Holder), n+1)
	}
	_, err := tx.ExecContext(ctx, query, args(lease.Key, lease.Holder)...)
	return err
}
