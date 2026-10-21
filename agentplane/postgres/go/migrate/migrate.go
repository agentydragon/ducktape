// Package migrate runs a service's database migrations from its migrator image.
package migrate

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"time"

	_ "github.com/lib/pq"
	"github.com/sethvargo/go-retry"
)

// connectBound is how long the first connection is retried before the migrator exits and the kubelet's restart
// back-off takes over.
const connectBound = 2 * time.Minute

// Run applies up to the PostgreSQL database at databaseURL.
//
// A migrator often starts alongside its database, so every failure of the first connection is retried in
// process, each one logged, until connectBound has passed.
func Run(ctx context.Context, databaseURL string, up func(context.Context, *sql.DB) error) error {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	backoff := retry.WithMaxDuration(connectBound, retry.WithCappedDuration(time.Second, retry.NewExponential(100*time.Millisecond)))
	if err := retry.Do(ctx, backoff, func(ctx context.Context) error {
		if err := db.PingContext(ctx); err != nil {
			slog.WarnContext(ctx, "PostgreSQL is not accepting connections yet", "err", err)
			return retry.RetryableError(err)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("connect to PostgreSQL: %w", err)
	}
	return up(ctx, db)
}
