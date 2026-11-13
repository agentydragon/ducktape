package github

import (
	"context"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
)

// Maintenance expires GitHub's cached receipts and observations. It makes no GitHub API calls,
// and runs even when the GitHub source is disabled. Published inbox content has its own lifetime.
type Maintenance struct {
	Store     *store.Store
	Retention settings.Retention
}

// Cleanup deletes one bounded page of expired GitHub state.
func (m Maintenance) Cleanup(ctx context.Context) error {
	if m.Retention.Days == nil {
		return nil
	}
	return m.Store.CleanupGitHubDeliveries(ctx, time.Now().Add(-time.Duration(*m.Retention.Days)*24*time.Hour))
}

func (m Maintenance) Run(ctx context.Context) error {
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-timer.C:
		}
		delay := time.Minute
		if err := m.Cleanup(ctx); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			slog.WarnContext(ctx, "GitHub retention unavailable; retrying", "error", err)
			delay = 5 * time.Second
		}
		timer.Reset(delay)
	}
}
