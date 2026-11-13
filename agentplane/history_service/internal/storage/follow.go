package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres"
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
	err := postgres.Transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
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
			upper, remaining := last, uint64(start.Back.GetCount())
			for remaining > 0 {
				count := int(min(remaining, 1000))
				references, err := eventReferences(ctx, tx, sessionID, 0, upper, []historypb.EventKind{start.Back.GetKind()}, count, true)
				if err != nil {
					return err
				}
				if len(references) < count {
					return nil
				}
				upper = references[len(references)-1].cursor - 1
				remaining -= uint64(count)
			}
			after = upper
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

// ReadFollowEntries reads a bounded page and its settlements from one snapshot. Size accounting
// reserves room for the response envelope and each repeated entry's wire header; an oversized
// entry is returned alone. No database transaction is held while the caller sends the page.
func (s *Store) ReadFollowEntries(ctx context.Context, sessionID uuid.UUID, after uint64, kinds []historypb.EventKind, includeSettlements bool) (FollowPage, error) {
	var page FollowPage
	err := postgres.Transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		var err error
		if page.LastCursor, err = s.followLastCursor(ctx, tx, sessionID); err != nil {
			return err
		}
		if after > page.LastCursor {
			return ErrOutOfRange
		}
		for _, kind := range kinds {
			if !validFollowKind(kind) {
				return invalid("event kind")
			}
		}
		references, err := eventReferences(ctx, tx, sessionID, after, page.LastCursor, kinds, FollowEntriesLimit+1, false)
		if err != nil {
			return err
		}
		count, size := 0, 32
		for _, reference := range references {
			if count == FollowEntriesLimit || (count > 0 && size+reference.size+6 > FollowBytesLimit) {
				break
			}
			count++
			size += reference.size + 6
		}
		if page.Entries, err = loadEntries(ctx, tx, sessionID, references[:count]); err != nil {
			return err
		}
		page.ThroughCursor = page.LastCursor
		if count < len(references) {
			page.ThroughCursor = references[count-1].cursor
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
