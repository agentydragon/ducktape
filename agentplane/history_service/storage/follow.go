package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const (
	FollowEntriesLimit     = 128
	FollowSettlementsLimit = 128
	FollowBytesLimit       = 1 << 20
)

// ErrOutOfRange means a resume cursor is beyond the Session's committed prefix.
var ErrOutOfRange = errors.New("cursor beyond committed history")

// FollowPage covers every cursor after the requested cursor through ThroughCursor, including kinds
// filtered out and entries accounted for by settlement/removal metadata. LastCursor is the committed
// tail in the same snapshot, so equality with ThroughCursor means this page caught up.
type FollowPage struct {
	Entries       []*eventlogpb.EventEntry
	Settlements   []*historypb.SettledDeltas
	ThroughCursor uint64
	LastCursor    uint64
}

func validFollowKind(kind historypb.EventKind) bool {
	_, exists := historypb.EventKind_name[int32(kind)]
	return kind != historypb.EventKind_EVENT_KIND_UNSPECIFIED && exists
}

// ResolveFollowStart resolves the inclusive back start into an exclusive resume cursor. Tail is
// resolved against one committed snapshot, so subsequent reads include every later commit.
func (s *Store) ResolveFollowStart(ctx context.Context, sessionID uuid.UUID, request *historypb.FollowSessionRequest) (uint64, error) {
	for _, kind := range request.GetKinds() {
		if !validFollowKind(kind) {
			return 0, invalid("event kind")
		}
	}
	switch start := request.GetStart().(type) {
	case *historypb.FollowSessionRequest_AfterCursor:
	case *historypb.FollowSessionRequest_Back:
		if start.Back.GetCount() == 0 || !validFollowKind(start.Back.GetKind()) {
			return 0, invalid("back start")
		}
	case *historypb.FollowSessionRequest_Tail:
	default:
		return 0, invalid("missing follow start")
	}
	var after uint64
	err := transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		last, err := s.followLastCursor(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		switch start := request.GetStart().(type) {
		case *historypb.FollowSessionRequest_AfterCursor:
			if start.AfterCursor > last {
				return ErrOutOfRange
			}
			after = start.AfterCursor
		case *historypb.FollowSessionRequest_Tail:
			after = last
		case *historypb.FollowSessionRequest_Back:
			var cursor uint64
			err := tx.QueryRowContext(ctx, `SELECT cursor FROM session_event
				WHERE session_id = $1 AND kind = $2 AND cursor <= $3
				ORDER BY cursor DESC OFFSET $4 LIMIT 1`,
				sessionID, start.Back.GetKind(), last, uint64(start.Back.GetCount())-1).Scan(&cursor)
			if errors.Is(err, sql.ErrNoRows) {
				return nil
			}
			if err != nil {
				return err
			}
			after = cursor - 1
		}
		return nil
	})
	return after, err
}

func (s *Store) followLastCursor(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) (uint64, error) {
	var last uint64
	predicate, scopeArgs := s.sessionPredicate("h", 2)
	args := append([]any{sessionID}, scopeArgs...)
	err := tx.QueryRowContext(ctx, "SELECT h.last_cursor FROM session_history h WHERE h.id = $1 AND "+predicate, args...).Scan(&last)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, fmt.Errorf("%w: %s", ErrNotFound, sessionID)
	}
	return last, err
}

// followCandidates seeks a bounded slice of each requested kind's index before merging by cursor.
// A single IN filter with cursor ordering can instead scan an arbitrarily long excluded prefix.
func followCandidates(kinds []historypb.EventKind, args *[]any) (string, error) {
	base := `SELECT cursor, octet_length(payload) + 6 AS size FROM session_event
		WHERE session_id = $1 AND cursor > $2 AND cursor <= $3`
	if len(kinds) == 0 {
		return fmt.Sprintf("%s ORDER BY cursor LIMIT %d", base, FollowEntriesLimit+1), nil
	}
	var parameters []string
	seen := make(map[historypb.EventKind]bool)
	for _, kind := range kinds {
		if !validFollowKind(kind) {
			return "", invalid("event kind")
		}
		if seen[kind] {
			continue
		}
		seen[kind] = true
		*args = append(*args, kind)
		parameters = append(parameters, fmt.Sprintf("($%d::integer)", len(*args)))
	}
	return fmt.Sprintf(`SELECT candidate.cursor, candidate.size FROM (VALUES %s) requested(kind)
		CROSS JOIN LATERAL (%s AND kind = requested.kind ORDER BY cursor LIMIT %d) candidate
		ORDER BY candidate.cursor LIMIT %d`, strings.Join(parameters, ","), base, FollowEntriesLimit+1, FollowEntriesLimit+1), nil
}

// ReadFollowEntries reads a bounded page and its settlements from one snapshot. Size accounting
// reserves room for the response envelope and each repeated entry's wire header; an oversized
// entry is returned alone. No database transaction is held while the caller sends the page.
func (s *Store) ReadFollowEntries(ctx context.Context, sessionID uuid.UUID, after uint64, kinds []historypb.EventKind, includeSettlements bool) (FollowPage, error) {
	var page FollowPage
	err := transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		var err error
		if page.LastCursor, err = s.followLastCursor(ctx, tx, sessionID); err != nil {
			return err
		}
		if after > page.LastCursor {
			return ErrOutOfRange
		}
		args := []any{sessionID, after, page.LastCursor}
		candidates, err := followCandidates(kinds, &args)
		if err != nil {
			return err
		}
		// Materialize lengths, not payloads: even many oversized candidate entries cannot turn into
		// an oversized result. The extra candidate tells us whether this frame covers the tail.
		query := fmt.Sprintf(`WITH candidates AS MATERIALIZED (%s), sized AS MATERIALIZED (
			SELECT cursor, row_number() OVER (ORDER BY cursor) AS position,
				sum(size) OVER (ORDER BY cursor) AS bytes FROM candidates
		), selected AS MATERIALIZED (
			SELECT cursor FROM sized WHERE position <= %d AND (bytes <= %d OR position = 1)
		)
		SELECT selected.cursor, event.payload,
			(SELECT max(cursor) FROM candidates) > (SELECT max(cursor) FROM selected) AS more
		FROM selected JOIN session_event event ON event.session_id = $1 AND event.cursor = selected.cursor
		ORDER BY selected.cursor`, candidates, FollowEntriesLimit, FollowBytesLimit-32)
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		page.ThroughCursor = page.LastCursor
		for rows.Next() {
			var cursor uint64
			var payload []byte
			var more bool
			if err := rows.Scan(&cursor, &payload, &more); err != nil {
				return err
			}
			entry := &eventlogpb.EventEntry{}
			if err := proto.Unmarshal(payload, entry); err != nil {
				return err
			}
			page.Entries = append(page.Entries, entry)
			if more {
				page.ThroughCursor = cursor
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		if includeSettlements {
			settlements, err := tx.QueryContext(ctx, `SELECT completion_cursor, payload FROM session_event_settlement
				WHERE session_id = $1 AND completion_cursor > $2 AND completion_cursor <= $3
				ORDER BY completion_cursor LIMIT $4`, sessionID, after, page.ThroughCursor, FollowSettlementsLimit+1)
			if err != nil {
				return err
			}
			var overflowCursor uint64
			for settlements.Next() {
				var completionCursor uint64
				var payload []byte
				if err := settlements.Scan(&completionCursor, &payload); err != nil {
					settlements.Close()
					return err
				}
				if len(page.Settlements) == FollowSettlementsLimit {
					overflowCursor = completionCursor
					break
				}
				settled := &historypb.SettledDeltas{}
				if err := proto.Unmarshal(payload, settled); err != nil {
					settlements.Close()
					return err
				}
				page.Settlements = append(page.Settlements, settled)
			}
			if err := settlements.Err(); err != nil {
				settlements.Close()
				return err
			}
			if err := settlements.Close(); err != nil {
				return err
			}
			if overflowCursor != 0 {
				page.ThroughCursor = overflowCursor - 1
				entries := page.Entries[:0]
				for _, entry := range page.Entries {
					if entry.GetCursor() <= page.ThroughCursor {
						entries = append(entries, entry)
					}
				}
				page.Entries = entries
			}
		}
		return nil
	})
	return page, err
}

// ReadFollowRemoved returns every removal intersecting the covered window in one query. Cleanup can
// remove a range behind the stream's cursor at any time, so each catch-up checks the full window.
func (s *Store) ReadFollowRemoved(ctx context.Context, sessionID uuid.UUID, coveredAfter, through uint64) ([]*historypb.CursorRange, error) {
	predicate, scopeArgs := s.sessionPredicate("h", 4)
	args := []any{sessionID, coveredAfter, through}
	args = append(args, scopeArgs...)
	rows, err := s.db.QueryContext(ctx, `SELECT removed.first_cursor, removed.last_cursor FROM session_event_removed_range removed
		WHERE removed.session_id = $1 AND removed.first_cursor <= $3 AND removed.last_cursor > $2
		AND EXISTS (SELECT FROM session_history h WHERE h.id = removed.session_id AND `+predicate+`)
		ORDER BY removed.first_cursor`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var removed []*historypb.CursorRange
	for rows.Next() {
		cursorRange := &historypb.CursorRange{}
		if err := rows.Scan(&cursorRange.First, &cursorRange.Last); err != nil {
			return nil, err
		}
		removed = append(removed, cursorRange)
	}
	return removed, rows.Err()
}
