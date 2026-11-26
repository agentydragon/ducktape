package github

import (
	"context"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication/fanout"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
)

// Maintenance expires GitHub's optional webhook bodies, cached receipts and observations. It makes no GitHub API
// calls, and runs even when the GitHub source is disabled. Published inbox content has its own lifetime.
type Maintenance struct {
	Fanout    *fanout.Coordinator
	Retention settings.Retention
}

// Cleanup deletes one bounded page of expired GitHub state and expires optional bodies independently.
func (m Maintenance) Cleanup(ctx context.Context) error {
	now := time.Now()
	if m.Retention.PayloadDays != nil {
		cutoff := now.Add(-time.Duration(*m.Retention.PayloadDays) * 24 * time.Hour)
		if err := m.Fanout.CleanupPayloads(ctx, cutoff); err != nil {
			return err
		}
	}
	if m.Retention.Days != nil {
		return m.Fanout.Cleanup(ctx, now.Add(-time.Duration(*m.Retention.Days)*24*time.Hour))
	}
	return nil
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
