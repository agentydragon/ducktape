// Package journalstats measures encoded journal storage without reading events into memory.
package journalstats

import (
	"context"
	"database/sql"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	pb "github.com/agentydragon/ducktape/agentplane/proto/journalstatspb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Only service-owned table names can enter the query.
func Sampler(db *sql.DB) *service { return newService(db, "sampler_session", "sampler_event_block") }

func History(db *sql.DB) *service { return newService(db, "session_history", "session_event_block") }

type service struct {
	pb.UnimplementedJournalStatsServiceServer
	db    *sql.DB
	query string
}

func newService(db *sql.DB, sessions, blocks string) *service {
	return &service{db: db, query: `SELECT statement_timestamp(), (SELECT count(*) FROM ` + sessions + `),
 count(*), coalesce(sum(octet_length(payload)), 0),
 count(DISTINCT session_id) FILTER (WHERE block_format = 'legacy_gzip'),
 count(*) FILTER (WHERE block_format = 'legacy_gzip'),
 coalesce(sum(octet_length(payload)) FILTER (WHERE block_format = 'legacy_gzip'), 0),
 count(*) FILTER (WHERE block_format IN ('legacy_gzip', 'apb_v1') AND uncompressed_bytes IS NOT NULL),
 count(*) FILTER (WHERE block_format = 'apb_v1'),
 coalesce(sum(octet_length(payload)) FILTER (WHERE block_format = 'apb_v1'), 0),
 CASE WHEN count(*) FILTER (WHERE block_format = 'apb_v1' AND uncompressed_bytes IS NULL) = 0
      THEN coalesce(sum(uncompressed_bytes) FILTER (WHERE block_format = 'apb_v1'), 0) END,
 CASE WHEN count(*) FILTER (WHERE block_format = 'legacy_gzip' AND uncompressed_bytes IS NULL) = 0
      THEN coalesce(sum(uncompressed_bytes) FILTER (WHERE block_format = 'legacy_gzip'), 0) END
 FROM ` + blocks}
}

func (s *service) Measure(ctx context.Context, _ *emptypb.Empty) (*pb.JournalStats, error) {
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		return nil, err
	}
	ctx, err = rule.AdmitContext(ctx, 0)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var at time.Time
	m := new(pb.JournalStats)
	err = s.db.QueryRowContext(ctx, s.query).Scan(&at, &m.Sessions, &m.Blocks, &m.StoredBytes, &m.LegacySessions, &m.LegacyBlocks, &m.LegacyBytes, &m.ClassifiedBlocks, &m.ApbBlocks, &m.ApbBytes, &m.ApbUncompressedBytes, &m.LegacyUncompressedBytes)
	if err != nil {
		slog.WarnContext(ctx, "measure journal storage", "error", err)
		return nil, status.Error(codes.Unavailable, "journal measurement unavailable")
	}
	m.MeasuredAt = timestamppb.New(at)
	return m, nil
}
