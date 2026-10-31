// Package migrate initializes a service's PostgreSQL schema before it serves.
package migrate

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"

	_ "github.com/lib/pq"
	"github.com/sethvargo/go-retry"
)

const (
	connectBound   = 2 * time.Minute
	migrationBound = 2 * time.Minute
)

// RunOnly handles the service binary's optional migration-only invocation without requiring
// credentials or settings for its serving roles. Normal startup must still call Run.
func RunOnly(ctx context.Context, args []string, databaseURLEnv string, up func(context.Context, *sql.DB) error) (bool, error) {
	if !slices.Contains(args, "--migrate-only") {
		return false, nil
	}
	if len(args) != 1 {
		return true, fmt.Errorf("--migrate-only takes no other arguments; supply %s", databaseURLEnv)
	}
	databaseURL := os.Getenv(databaseURLEnv)
	if strings.TrimSpace(databaseURL) == "" {
		return true, fmt.Errorf("%s is required", databaseURLEnv)
	}
	return true, Run(ctx, databaseURL, up)
}

// Run connects with bounded retry, then applies versioned migrations before workers or listeners
// start. The owning provider serializes migration attempts through its stable advisory lock.
func Run(ctx context.Context, databaseURL string, up func(context.Context, *sql.DB) error) error {
	target, err := url.Parse(strings.Replace(databaseURL, "postgresql+asyncpg://", "postgresql://", 1))
	if err != nil || (target.Scheme != "postgresql" && target.Scheme != "postgres") {
		return fmt.Errorf("migration database_url must be a PostgreSQL URL")
	}
	query := target.Query()
	// Apply to every pooled migration connection, including registered Go backfills. This pool
	// is closed before serving; application queries do not inherit this DDL lock budget.
	query.Set("options", strings.TrimSpace(query.Get("options")+" -c lock_timeout=5s"))
	target.RawQuery = query.Encode()
	db, err := sql.Open("postgres", target.String())
	if err != nil {
		return err
	}
	defer db.Close()
	connectCtx, cancelConnect := context.WithTimeout(ctx, connectBound)
	defer cancelConnect()
	backoff := retry.WithMaxDuration(connectBound, retry.WithCappedDuration(time.Second, retry.NewExponential(100*time.Millisecond)))
	if err := retry.Do(connectCtx, backoff, func(ctx context.Context) error {
		if err := db.PingContext(ctx); err != nil {
			slog.WarnContext(ctx, "PostgreSQL is not accepting connections yet", "err", err)
			return retry.RetryableError(err)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("connect to PostgreSQL: %w", err)
	}
	cancelConnect()
	migrationCtx, cancelMigration := context.WithTimeout(ctx, migrationBound)
	defer cancelMigration()
	slog.InfoContext(ctx, "Applying database migrations")
	if err := up(migrationCtx, db); err != nil {
		return fmt.Errorf("apply database migrations: %w", err)
	}
	slog.InfoContext(ctx, "Database migrations complete")
	return nil
}
