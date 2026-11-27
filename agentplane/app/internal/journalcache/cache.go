// Package journalcache keeps slow, independent storage measurements off the HTTP path.
package journalcache

import (
	"context"
	"log/slog"
	"sync/atomic"
	"time"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	pb "github.com/agentydragon/ducktape/agentplane/proto/journalstatspb"
	"google.golang.org/protobuf/types/known/emptypb"
)

type Cache struct {
	sampler, history atomic.Pointer[pb.JournalStats]
}

func Start(ctx context.Context, sampler, history pb.JournalStatsServiceClient) *Cache {
	c := new(Cache)
	if sampler != nil {
		go collect(ctx, "sampler", sampler, &c.sampler)
	}
	if history != nil {
		go collect(ctx, "history", history, &c.history)
	}
	return c
}

func collect(ctx context.Context, name string, client pb.JournalStatsServiceClient, dest *atomic.Pointer[pb.JournalStats]) {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for {
		call, cancel := context.WithTimeout(ctx, 30*time.Second)
		sample, err := client.Measure(call, &emptypb.Empty{})
		cancel()
		if err == nil {
			dest.Store(sample)
		} else if ctx.Err() == nil {
			slog.WarnContext(ctx, "refresh journal stats", "store", name, "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Snapshot returns immutable last-known values; a failed refresh does not replace them.
func (c *Cache) Snapshot() *apppb.GetJournalStatsResponse {
	return &apppb.GetJournalStatsResponse{Sampler: c.sampler.Load(), History: c.history.Load()}
}
