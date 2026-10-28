// Package migrations owns Goose migrations for the Notification Service's `notifications` database.
package migrations

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"embed"
	"errors"
	"fmt"
	"io/fs"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations/backfill"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed notifications/*.sql
var scripts embed.FS

// Goose takes its lock on a separate connection, so the whole-cutover lock must use a different key.
const cutoverLockID int64 = 0x4E4F_5447

// Up completes the one-release cutover while the service is stopped by Recreate.
// A separate session lock covers every Goose phase and data copy, including init retries.
func Up(ctx context.Context, db *sql.DB) (retErr error) {
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer func() { retErr = errors.Join(retErr, conn.Close()) }()
	// A blocked pg_advisory_lock statement can hold a snapshot that CREATE INDEX CONCURRENTLY
	// must wait for. Let losing attempts finish their statements before retrying.
	lockRetry := time.NewTicker(100 * time.Millisecond)
	defer lockRetry.Stop()
	for {
		var locked bool
		if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, cutoverLockID).Scan(&locked); err != nil {
			// Cancellation can race lock acquisition; discard the session if ownership is uncertain.
			return errors.Join(fmt.Errorf("lock Notification cutover: %w", err), conn.Raw(func(any) error { return driver.ErrBadConn }))
		}
		if locked {
			break
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("wait for Notification cutover lock: %w", ctx.Err())
		case <-lockRetry.C:
		}
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx, `SELECT pg_advisory_unlock($1)`, cutoverLockID).Scan(&unlocked)
		if unlockErr == nil && !unlocked {
			unlockErr = errors.New("Notification cutover session did not own its advisory lock")
		}
		if unlockErr != nil {
			// An uncertain unlock must never return a lock-owning connection to the pool.
			unlockErr = errors.Join(unlockErr, conn.Raw(func(any) error { return driver.ErrBadConn }))
		}
		retErr = errors.Join(retErr, unlockErr)
	}()

	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	version, err := provider.GetDBVersion(ctx)
	if err != nil {
		return err
	}
	if version >= 20 {
		// Every proof was durable before 00020; completed init checks Goose metadata only.
		return UpTo(ctx, db, 23)
	}
	if version < 15 {
		if err := UpTo(ctx, db, 14); err != nil {
			return err
		}
		for _, step := range []struct {
			name string
			run  func(context.Context, *sql.DB) error
		}{
			{"inbox destinations", backfill.BackfillInboxDestinations},
			{"subscription creation", backfill.BackfillSubscriptionCreations},
			{"event identities", func(ctx context.Context, db *sql.DB) error { return backfill.BackfillEventIdentities(ctx, db, 512) }},
			{"entry content", func(ctx context.Context, db *sql.DB) error { return backfill.BackfillEntryContent(ctx, db, 512) }},
		} {
			if err := step.run(ctx, db); err != nil {
				return fmt.Errorf("backfill %s: %w", step.name, err)
			}
		}
	}
	if version < 19 {
		if err := UpTo(ctx, db, 19); err != nil {
			return err
		}
	}
	// Version 19 installs the write fence and checkpoint before cleanup touches old JSON.
	if err := backfill.CleanupEntryLegacyContent(ctx, db, 512); err != nil {
		return fmt.Errorf("clean legacy entry content: %w", err)
	}
	for _, step := range []struct {
		name string
		run  func(context.Context, *sql.DB) error
	}{
		{"inbox destination", backfill.ValidateInboxDestinationConstraints},
		{"subscription creation", backfill.ValidateSubscriptionCreationConstraints},
		{"event identity", backfill.ValidateEventIdentityWriterOnly},
	} {
		if err := step.run(ctx, db); err != nil {
			return fmt.Errorf("validate %s: %w", step.name, err)
		}
	}
	return UpTo(ctx, db, 23)
}

// UpTo applies authored SQL through version in the same provider used by Up. Integration tests use it to build a
// predecessor schema and exercise an upgrade against populated rows.
func UpTo(ctx context.Context, db *sql.DB, version int64) error {
	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	_, err = provider.UpTo(ctx, version)
	return err
}

func newProvider(db *sql.DB) (*goose.Provider, error) {
	// The advisory lock key the retired Alembic runner took, kept so that no two migrators run at once.
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4E4F_5446))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(scripts, "notifications")
	if err != nil {
		return nil, err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("notification_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return nil, err
	}
	return provider, nil
}
