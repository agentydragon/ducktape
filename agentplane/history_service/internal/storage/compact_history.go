package storage

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

var (
	// ErrCompactionSessionBusy marks a short lock timeout while acquiring the Session writer lock.
	ErrCompactionSessionBusy = errors.New("history repack Session is busy")
	errStaleRepackCandidate  = errors.New("history repack candidate changed")
)

const (
	repackUnpackPhase int16 = 0
	repackPackPhase   int16 = 1
	repackSessionPage       = 100
)

// CompactionResult reports logical work and stored payload bytes, excluding table and index pages.
type CompactionResult struct {
	Entries, Blocks, SourceBlocks, LegacyBlocks, VersionedSourceBlocks int64
	UncompressedBytes, PackedBytes, Skipped                            int64
}

// RepackBeforeBatch is called after a bounded candidate is prepared and before its short
// append-lock transaction begins. It is used for operator-controlled pacing and pause guards.
type RepackBeforeBatch func(context.Context, int64) error

type repackKindProfile struct {
	Kind          int    `json:"kind"`
	TargetBytes   int    `json:"target_bytes"`
	TargetEntries int    `json:"target_entries"`
	Codec         string `json:"codec"`
	Level         int    `json:"level"`
}

type repackProfile struct {
	FormatVersion int                 `json:"format_version"`
	Kinds         []repackKindProfile `json:"kinds"`
}

type repackCheckpoint struct {
	SessionID        uuid.UUID
	RecordedPosition uint64
	Through          uint64
	Start            uint64
	Kind             historypb.EventKind
	Phase            int16
	UnpackAfter      uint64
	PackAfter        uint64
	Complete         bool
}

type repackRun struct {
	OpeningPosition uint64
	AfterPosition   uint64
	Complete        bool
}

type repackRawMeta struct {
	cursor      uint64
	size        int
	indexSize   int
	storedBytes int64
}

type repackRawFrame struct {
	meta    repackRawMeta
	payload []byte
}

type repackBlock struct {
	storedBlock
	storedBytes int64
}

type repackPlan struct {
	frames  []repackRawFrame
	encoded []byte
	pack    bool
}

func (s *Store) repackProfile() ([]byte, [32]byte, []historypb.EventKind, error) {
	var kinds []historypb.EventKind
	for n := range historypb.EventKind_name {
		if n != 0 {
			kinds = append(kinds, historypb.EventKind(n))
		}
	}
	slices.Sort(kinds)
	profile := repackProfile{FormatVersion: 1}
	for _, kind := range kinds {
		packer := s.options.EventPackers[kind]
		if packer == nil {
			return nil, [32]byte{}, nil, fmt.Errorf("event block policy missing for History kind %s", kind)
		}
		p := packer.Profile()
		profile.Kinds = append(profile.Kinds, repackKindProfile{
			Kind: int(kind), TargetBytes: p.TargetBytes, TargetEntries: p.TargetEntries, Codec: "zstd", Level: p.Level,
		})
	}
	encoded, err := json.Marshal(profile)
	if err != nil {
		return nil, [32]byte{}, nil, err
	}
	return encoded, sha256.Sum256(encoded), kinds, nil
}

// CompactHistory retains the maintenance command's single-Session API. Apply checkpoints are
// keyed by the exact writer profile; dry-run reads and simulates the opening prefix without writes.
func (s *Store) CompactHistory(ctx context.Context, sessionID uuid.UUID, apply bool) (CompactionResult, error) {
	return s.RepackSession(ctx, sessionID, apply, nil)
}

// RepackSession repacks one canary Session using the configured writer profiles.
func (s *Store) RepackSession(ctx context.Context, sessionID uuid.UUID, apply bool, before RepackBeforeBatch) (CompactionResult, error) {
	_, hash, kinds, err := s.repackProfile()
	if err != nil {
		return CompactionResult{}, err
	}
	if !apply {
		return s.previewHistorySession(ctx, sessionID, kinds, before)
	}
	checkpoint, err := s.openRepackCheckpoint(ctx, hash, sessionID, kinds)
	if err != nil {
		return CompactionResult{}, err
	}
	return s.runRepackSession(ctx, hash, checkpoint, kinds, before)
}

// RepackAll enumerates session_history directly, so inactive Sessions remain in scope. The
// profile row snapshots the opening recording position; session cursors are captured once when
// each Session first enters the persisted checkpoint.
func (s *Store) RepackAll(ctx context.Context, apply bool, before RepackBeforeBatch) (CompactionResult, error) {
	profile, hash, kinds, err := s.repackProfile()
	if err != nil {
		return CompactionResult{}, err
	}
	if !apply {
		var opening uint64
		if err := s.db.QueryRowContext(ctx, `SELECT COALESCE(max(recorded_position),0) FROM session_history`).Scan(&opening); err != nil {
			return CompactionResult{}, err
		}
		var total CompactionResult
		for position := uint64(0); position < opening; {
			sessions, err := s.RecordedSessions(ctx, position, repackSessionPage)
			if err != nil {
				return total, err
			}
			if len(sessions) == 0 || sessions[0].Position > opening {
				break
			}
			for _, session := range sessions {
				if session.Position > opening {
					break
				}
				position = session.Position
				result, err := s.previewHistorySession(ctx, session.SessionID, kinds, before)
				addCompactionResult(&total, result)
				if err != nil {
					return total, err
				}
			}
		}
		return total, nil
	}
	run, err := s.beginRepackRun(ctx, hash, profile)
	if err != nil {
		return CompactionResult{}, err
	}
	var total CompactionResult
	for position := run.AfterPosition; position < run.OpeningPosition; {
		sessions, err := s.RecordedSessions(ctx, position, repackSessionPage)
		if err != nil {
			return total, err
		}
		if len(sessions) == 0 || sessions[0].Position > run.OpeningPosition {
			break
		}
		for _, session := range sessions {
			if session.Position > run.OpeningPosition {
				break
			}
			position = session.Position
			checkpoint, err := s.openRepackCheckpoint(ctx, hash, session.SessionID, kinds)
			if err != nil {
				return total, err
			}
			if err := s.advanceRepackRun(ctx, hash, position); err != nil {
				return total, err
			}
			result, err := s.runRepackSession(ctx, hash, checkpoint, kinds, before)
			addCompactionResult(&total, result)
			if errors.Is(err, ErrCompactionSessionBusy) {
				continue
			}
			if err != nil {
				return total, fmt.Errorf("repacking Session %s: %w", session.SessionID, err)
			}
		}
	}
	// Busy sessions are deferred while the run advances, then retried in bounded sweeps. If
	// contention remains, the run stays resumable and this call reports the busy condition.
	for attempt := 0; attempt < 3; attempt++ {
		pending, err := s.incompleteRepackSessions(ctx, hash, run.OpeningPosition)
		if err != nil {
			return total, err
		}
		if len(pending) == 0 {
			if err := s.finishRepackRun(ctx, hash); err != nil {
				return total, err
			}
			return total, nil
		}
		for _, session := range pending {
			checkpoint, err := s.loadRepackCheckpoint(ctx, hash, session.SessionID)
			if err != nil {
				return total, err
			}
			result, err := s.runRepackSession(ctx, hash, checkpoint, kinds, before)
			addCompactionResult(&total, result)
			if err != nil && !errors.Is(err, ErrCompactionSessionBusy) {
				return total, fmt.Errorf("retrying repack for Session %s: %w", session.SessionID, err)
			}
		}
		if attempt < 2 {
			timer := time.NewTimer(time.Duration(attempt+1) * 250 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return total, ctx.Err()
			case <-timer.C:
			}
		}
	}
	pending, err := s.incompleteRepackSessions(ctx, hash, run.OpeningPosition)
	if err != nil {
		return total, err
	}
	if len(pending) > 0 {
		return total, fmt.Errorf("%w: %d Sessions remain checkpointed for retry", ErrCompactionSessionBusy, len(pending))
	}
	return total, s.finishRepackRun(ctx, hash)
}

func addCompactionResult(total *CompactionResult, result CompactionResult) {
	total.Entries += result.Entries
	total.Blocks += result.Blocks
	total.SourceBlocks += result.SourceBlocks
	total.LegacyBlocks += result.LegacyBlocks
	total.VersionedSourceBlocks += result.VersionedSourceBlocks
	total.UncompressedBytes += result.UncompressedBytes
	total.PackedBytes += result.PackedBytes
	total.Skipped += result.Skipped
}

func (s *Store) beginRepackRun(ctx context.Context, hash [32]byte, profile []byte) (repackRun, error) {
	var run repackRun
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		lockKey := int64(binary.BigEndian.Uint64(hash[:8]))
		if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, lockKey); err != nil {
			return err
		}
		var savedProfile []byte
		err := tx.QueryRowContext(ctx, `SELECT opening_position,after_position,complete,profile
			FROM history_event_repack_run WHERE profile_hash=$1 FOR UPDATE`, hash[:]).
			Scan(&run.OpeningPosition, &run.AfterPosition, &run.Complete, &savedProfile)
		if err == nil && !run.Complete {
			var saved repackProfile
			if json.Unmarshal(savedProfile, &saved) != nil {
				return errors.New("stored History repack profile is invalid")
			}
			normalized, marshalErr := json.Marshal(saved)
			if marshalErr != nil || !bytes.Equal(normalized, profile) {
				return errors.New("History repack profile hash collision")
			}
			return nil
		}
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err := tx.QueryRowContext(ctx, `SELECT COALESCE(max(recorded_position),0) FROM session_history`).Scan(&run.OpeningPosition); err != nil {
			return err
		}
		run.AfterPosition, run.Complete = 0, false
		_, err = tx.ExecContext(ctx, `INSERT INTO history_event_repack_run(profile_hash,profile,opening_position,after_position,complete)
			VALUES($1,$2::jsonb,$3,0,false)
			ON CONFLICT(profile_hash) DO UPDATE SET profile=EXCLUDED.profile,opening_position=EXCLUDED.opening_position,
				after_position=0,complete=false,updated_at=now()`, hash[:], string(profile), run.OpeningPosition)
		return err
	})
	return run, err
}

func (s *Store) advanceRepackRun(ctx context.Context, hash [32]byte, position uint64) error {
	_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_run SET after_position=GREATEST(after_position,$2),updated_at=now()
		WHERE profile_hash=$1 AND NOT complete`, hash[:], position)
	return err
}

func (s *Store) finishRepackRun(ctx context.Context, hash [32]byte) error {
	_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_run SET complete=true,updated_at=now()
		WHERE profile_hash=$1 AND NOT EXISTS (
			SELECT FROM history_event_repack_checkpoint c WHERE c.profile_hash=$1 AND NOT c.complete)`, hash[:])
	return err
}

func (s *Store) incompleteRepackSessions(ctx context.Context, hash [32]byte, opening uint64) ([]RecordedSession, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT session_id,recorded_position FROM history_event_repack_checkpoint
		WHERE profile_hash=$1 AND recorded_position<=$2 AND NOT complete ORDER BY recorded_position LIMIT $3`,
		hash[:], opening, repackSessionPage)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []RecordedSession
	for rows.Next() {
		var session RecordedSession
		if err := rows.Scan(&session.SessionID, &session.Position); err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func (s *Store) openRepackCheckpoint(ctx context.Context, hash [32]byte, sessionID uuid.UUID, kinds []historypb.EventKind) (repackCheckpoint, error) {
	var checkpoint repackCheckpoint
	checkpoint.SessionID = sessionID
	var lastCursor uint64
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if err := tx.QueryRowContext(ctx, `SELECT recorded_position,last_cursor FROM session_history WHERE id=$1`, sessionID).
			Scan(&checkpoint.RecordedPosition, &lastCursor); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO history_event_repack_checkpoint
			(profile_hash,session_id,recorded_position,through_cursor,event_kind,phase,unpack_after,pack_after,complete)
			VALUES($1,$2,$3,$4,$5,$6,0,0,false) ON CONFLICT(profile_hash,session_id) DO NOTHING`,
			hash[:], sessionID, checkpoint.RecordedPosition, lastCursor, kinds[0], repackUnpackPhase)
		if err != nil {
			return err
		}
		if err := tx.QueryRowContext(ctx, `SELECT recorded_position,through_cursor,start_cursor,event_kind,phase,unpack_after,pack_after,complete
			FROM history_event_repack_checkpoint WHERE profile_hash=$1 AND session_id=$2 FOR UPDATE`, hash[:], sessionID).
			Scan(&checkpoint.RecordedPosition, &checkpoint.Through, &checkpoint.Start, &checkpoint.Kind, &checkpoint.Phase,
				&checkpoint.UnpackAfter, &checkpoint.PackAfter, &checkpoint.Complete); err != nil {
			return err
		}
		if checkpoint.Complete && lastCursor > checkpoint.Through {
			previousThrough := checkpoint.Through
			checkpoint.Through = lastCursor
			checkpoint.Start = previousThrough
			checkpoint.Kind, checkpoint.Phase = kinds[0], repackUnpackPhase
			checkpoint.UnpackAfter, checkpoint.PackAfter, checkpoint.Complete = checkpoint.Start, checkpoint.Start, false
			_, err := tx.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET recorded_position=$3,through_cursor=$4,
				start_cursor=$5,event_kind=$6,phase=$7,unpack_after=$8,pack_after=$8,complete=false,updated_at=now()
				WHERE profile_hash=$1 AND session_id=$2`, hash[:], sessionID, checkpoint.RecordedPosition, checkpoint.Through,
				checkpoint.Start, checkpoint.Kind, checkpoint.Phase, checkpoint.Start)
			return err
		}
		return nil
	})
	return checkpoint, err
}

func (s *Store) loadRepackCheckpoint(ctx context.Context, hash [32]byte, sessionID uuid.UUID) (repackCheckpoint, error) {
	var checkpoint repackCheckpoint
	checkpoint.SessionID = sessionID
	err := s.db.QueryRowContext(ctx, `SELECT recorded_position,through_cursor,start_cursor,event_kind,phase,unpack_after,pack_after,complete
		FROM history_event_repack_checkpoint WHERE profile_hash=$1 AND session_id=$2`, hash[:], sessionID).
		Scan(&checkpoint.RecordedPosition, &checkpoint.Through, &checkpoint.Start, &checkpoint.Kind, &checkpoint.Phase,
			&checkpoint.UnpackAfter, &checkpoint.PackAfter, &checkpoint.Complete)
	return checkpoint, err
}

func (s *Store) runRepackSession(ctx context.Context, hash [32]byte, checkpoint repackCheckpoint, kinds []historypb.EventKind, before RepackBeforeBatch) (CompactionResult, error) {
	var result CompactionResult
	for !checkpoint.Complete {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		var err error
		if checkpoint.Phase == repackUnpackPhase {
			var block repackBlock
			var found bool
			block, found, err = s.nextRepackBlock(ctx, checkpoint)
			if err != nil {
				return result, err
			}
			if !found {
				if err := s.switchRepackToPack(ctx, hash, checkpoint.SessionID); err != nil {
					return result, err
				}
				checkpoint.Phase = repackPackPhase
				continue
			}
			if before != nil {
				if err := before(ctx, int64(len(block.payload))); err != nil {
					return result, err
				}
			}
			err = s.unpackRepackBlock(ctx, hash, checkpoint, block)
			if errors.Is(err, errStaleRepackCandidate) {
				continue
			}
			if err != nil {
				return result, err
			}
			checkpoint.PackAfter = min(checkpoint.PackAfter, block.first-1)
			result.SourceBlocks++
			if eventblock.IsBlock(block.payload) {
				result.VersionedSourceBlocks++
			} else {
				result.LegacyBlocks++
			}
			checkpoint.UnpackAfter = block.last
			continue
		}
		if checkpoint.Phase != repackPackPhase {
			return result, fmt.Errorf("unknown History repack phase %d", checkpoint.Phase)
		}
		_, found, err := s.findRepackBlockAfter(ctx, checkpoint.SessionID, checkpoint.Kind, checkpoint.PackAfter, checkpoint.Through)
		if err != nil {
			return result, err
		}
		if found {
			if err := s.restartRepackUnpack(ctx, hash, checkpoint); err != nil {
				return result, err
			}
			checkpoint.Phase = repackUnpackPhase
			checkpoint.UnpackAfter = min(checkpoint.UnpackAfter, checkpoint.PackAfter)
			continue
		}
		metas, hasMore, err := s.nextRepackRawMeta(ctx, checkpoint)
		if err != nil {
			return result, err
		}
		if len(metas) == 0 {
			checkpoint, err = s.advanceRepackKind(ctx, hash, checkpoint, kinds)
			if err != nil {
				return result, err
			}
			continue
		}
		packer := s.options.EventPackers[checkpoint.Kind]
		sizes := make([]int, len(metas))
		for i, meta := range metas {
			sizes[i] = meta.size
		}
		cut := packer.NextSizes(sizes, !hasMore)
		if cut.N < 1 {
			return result, errors.New("event block profile did not advance a bounded repack batch")
		}
		frames, err := s.loadRepackRawFrames(ctx, checkpoint, metas[:cut.N])
		if err != nil {
			if errors.Is(err, errStaleRepackCandidate) {
				continue
			}
			return result, err
		}
		plan, err := buildRepackPlan(packer, frames, cut)
		if err != nil {
			return result, err
		}
		var sourceBytes int64
		for _, frame := range frames {
			sourceBytes += int64(frame.meta.size)
		}
		if before != nil {
			if err := before(ctx, sourceBytes); err != nil {
				return result, err
			}
		}
		last := frames[len(frames)-1].meta.cursor
		err = s.applyRepackPlan(ctx, hash, checkpoint, plan, last)
		if errors.Is(err, errStaleRepackCandidate) {
			continue
		}
		if err != nil {
			return result, err
		}
		result.Entries += int64(len(frames))
		for _, frame := range frames {
			result.UncompressedBytes += int64(frame.meta.size)
		}
		if plan.pack {
			result.Blocks++
			result.PackedBytes += int64(len(plan.encoded))
		} else {
			result.Skipped += int64(len(frames))
		}
		checkpoint.PackAfter = last
	}
	return result, nil
}

func (s *Store) nextRepackBlock(ctx context.Context, checkpoint repackCheckpoint) (repackBlock, bool, error) {
	return s.findRepackBlock(ctx, checkpoint.SessionID, checkpoint.Kind, checkpoint.UnpackAfter, checkpoint.Through)
}

func (s *Store) findRepackBlockAfter(ctx context.Context, sessionID uuid.UUID, kind historypb.EventKind, after, through uint64) (repackBlock, bool, error) {
	return s.findRepackBlock(ctx, sessionID, kind, after, through)
}

func (s *Store) findRepackBlock(ctx context.Context, sessionID uuid.UUID, kind historypb.EventKind, after, through uint64) (repackBlock, bool, error) {
	block, err := scanStoredBlock(s.db.QueryRowContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
		FROM session_event_block WHERE session_id=$1 AND kind=$2 AND first_cursor<=$3 AND last_cursor>$4
		ORDER BY first_cursor LIMIT 1`, sessionID, kind, through, after))
	if errors.Is(err, sql.ErrNoRows) {
		return repackBlock{}, false, nil
	}
	if err != nil {
		return repackBlock{}, false, err
	}
	return repackBlock{storedBlock: block, storedBytes: block.storedBytes}, true, nil
}

func (s *Store) switchRepackToPack(ctx context.Context, hash [32]byte, sessionID uuid.UUID) error {
	_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET phase=$3,updated_at=now()
		WHERE profile_hash=$1 AND session_id=$2 AND NOT complete`, hash[:], sessionID, repackPackPhase)
	return err
}

func (s *Store) restartRepackUnpack(ctx context.Context, hash [32]byte, checkpoint repackCheckpoint) error {
	_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET phase=$3,
		unpack_after=LEAST(unpack_after,pack_after),updated_at=now()
		WHERE profile_hash=$1 AND session_id=$2 AND NOT complete`, hash[:], checkpoint.SessionID, repackUnpackPhase)
	return err
}

func (s *Store) advanceRepackKind(ctx context.Context, hash [32]byte, checkpoint repackCheckpoint, kinds []historypb.EventKind) (repackCheckpoint, error) {
	index := slices.Index(kinds, checkpoint.Kind)
	if index < 0 {
		return checkpoint, fmt.Errorf("History repack checkpoint has unknown kind %d", checkpoint.Kind)
	}
	if index+1 == len(kinds) {
		_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET complete=true,updated_at=now()
			WHERE profile_hash=$1 AND session_id=$2`, hash[:], checkpoint.SessionID)
		checkpoint.Complete = true
		return checkpoint, err
	}
	next := kinds[index+1]
	_, err := s.db.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET event_kind=$3,phase=$4,
		unpack_after=$5,pack_after=$5,updated_at=now() WHERE profile_hash=$1 AND session_id=$2`,
		hash[:], checkpoint.SessionID, next, repackUnpackPhase, checkpoint.Start)
	checkpoint.Kind, checkpoint.Phase = next, repackUnpackPhase
	checkpoint.UnpackAfter, checkpoint.PackAfter = checkpoint.Start, checkpoint.Start
	return checkpoint, err
}

func (s *Store) unpackRepackBlock(ctx context.Context, hash [32]byte, checkpoint repackCheckpoint, block repackBlock) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout='250ms'`); err != nil {
			return err
		}
		if _, err := lockRow(ctx, tx, checkpoint.SessionID); err != nil {
			var pgErr *pq.Error
			if errors.As(err, &pgErr) && pgErr.Code == "55P03" {
				return fmt.Errorf("%w: %w", ErrCompactionSessionBusy, err)
			}
			return err
		}
		current, err := scanStoredBlock(tx.QueryRowContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
			FROM session_event_block WHERE session_id=$1 AND first_cursor=$2`, checkpoint.SessionID, block.first))
		if errors.Is(err, sql.ErrNoRows) || err == nil && !sameStoredBlock(current, block.storedBlock) {
			return errStaleRepackCandidate
		}
		if err != nil {
			return err
		}
		cursors := make([]int64, len(block.frames))
		for i, frame := range block.frames {
			cursors[i] = int64(frame.cursor)
		}
		var removed bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(
			SELECT FROM session_event_removed_range r WHERE r.session_id=$1 AND EXISTS
				(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor)
			UNION ALL
			SELECT FROM session_event_settled_range r WHERE r.session_id=$1 AND EXISTS
				(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor))`,
			checkpoint.SessionID, pq.Array(cursors)).Scan(&removed); err != nil {
			return err
		}
		if removed {
			return fmt.Errorf("event block %d still contains a removed or settled cursor", block.first)
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM session_event_block WHERE session_id=$1 AND first_cursor=$2`, checkpoint.SessionID, block.first); err != nil {
			return err
		}
		for _, frame := range block.frames {
			if err := insertRawFrame(ctx, tx, checkpoint.SessionID, frame); err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET unpack_after=GREATEST(unpack_after,$3),
			pack_after=LEAST(pack_after,$4),updated_at=now() WHERE profile_hash=$1 AND session_id=$2`,
			hash[:], checkpoint.SessionID, block.last, block.first-1)
		return err
	})
}

func sameStoredBlock(a, b storedBlock) bool {
	return a.first == b.first && a.last == b.last && a.kind == b.kind && slices.Equal(a.cursors, b.cursors) &&
		slices.Equal(a.sizes, b.sizes) && bytes.Equal(a.payload, b.payload)
}

func (s *Store) nextRepackRawMeta(ctx context.Context, checkpoint repackCheckpoint) ([]repackRawMeta, bool, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT cursor,octet_length(payload),pg_column_size(payload) FROM session_event
		WHERE session_id=$1 AND kind=$2 AND cursor>$3 AND cursor<=$4 ORDER BY cursor LIMIT $5`,
		checkpoint.SessionID, checkpoint.Kind, checkpoint.PackAfter, checkpoint.Through, eventblock.MaxEntries+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	var metas []repackRawMeta
	var total int
	more := false
	for rows.Next() {
		var meta repackRawMeta
		if err := rows.Scan(&meta.cursor, &meta.size, &meta.storedBytes); err != nil {
			return nil, false, err
		}
		if len(metas) >= eventblock.MaxEntries || len(metas) > 0 && total+meta.size+4 > eventblock.MaxBytes {
			more = true
			metas = append(metas, meta) // one length-only lookahead lets NextSizes seal before the limit.
			break
		}
		metas = append(metas, meta)
		total += meta.size + 4
		if meta.size+4 > eventblock.MaxBytes {
			more = true
			break
		}
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	if len(metas) == eventblock.MaxEntries+1 {
		more = true
	}
	// A short page is the only case where the final tail can be flushed with certainty.
	if !more && len(metas) == eventblock.MaxEntries+1 {
		more = true
	}
	return metas, more, nil
}

func (s *Store) loadRepackRawFrames(ctx context.Context, checkpoint repackCheckpoint, metas []repackRawMeta) ([]repackRawFrame, error) {
	if len(metas) == 1 && metas[0].size+4 > eventblock.MaxBytes {
		// Oversized frames stay ordinary; their bytes need not be copied into process memory.
		return []repackRawFrame{{meta: metas[0]}}, nil
	}
	cursors := make([]int64, len(metas))
	byCursor := make(map[uint64]repackRawMeta, len(metas))
	for i, meta := range metas {
		cursors[i] = int64(meta.cursor)
		byCursor[meta.cursor] = meta
	}
	rows, err := s.db.QueryContext(ctx, `SELECT cursor,kind,payload FROM session_event
		WHERE session_id=$1 AND cursor=ANY($2) ORDER BY cursor`, checkpoint.SessionID, pq.Array(cursors))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var frames []repackRawFrame
	for rows.Next() {
		var cursor uint64
		var kind historypb.EventKind
		var payload []byte
		if err := rows.Scan(&cursor, &kind, &payload); err != nil {
			return nil, err
		}
		meta, ok := byCursor[cursor]
		if !ok || len(payload) != meta.size || kind != checkpoint.Kind {
			return nil, errStaleRepackCandidate
		}
		entry, err := unmarshalRawEntry(payload)
		if err != nil || entry.GetCursor() != cursor || historypb.EventKind(observationNumber(entry)) != kind {
			return nil, fmt.Errorf("ordinary History row %d disagrees with its index", cursor)
		}
		meta.indexSize = proto.Size(entry)
		frames = append(frames, repackRawFrame{meta: meta, payload: bytes.Clone(payload)})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(frames) != len(metas) {
		return nil, errStaleRepackCandidate
	}
	return frames, nil
}

func unmarshalRawEntry(payload []byte) (*eventlogpb.EventEntry, error) {
	entry := new(eventlogpb.EventEntry)
	if err := proto.Unmarshal(payload, entry); err != nil {
		return nil, err
	}
	if entry.GetCursor() == 0 || entry.GetEvent() == nil {
		return nil, errors.New("invalid event row")
	}
	return entry, nil
}

func buildRepackPlan(packer *eventblock.Packer, frames []repackRawFrame, cut eventblock.Cut) (repackPlan, error) {
	plan := repackPlan{frames: frames}
	if cut.Decision != eventblock.Seal || cut.N != len(frames) {
		return plan, nil
	}
	wire := make([][]byte, len(frames))
	var oldBytes int64
	for i, frame := range frames {
		wire[i] = frame.payload
		oldBytes += frame.meta.storedBytes
	}
	encoded, err := packer.Encode(wire)
	if err != nil {
		return plan, err
	}
	decoded, err := eventblock.Decode(encoded)
	if err != nil || len(decoded) != len(wire) {
		return plan, errors.New("new event block failed round-trip validation")
	}
	for i := range wire {
		if !bytes.Equal(decoded[i], wire[i]) {
			return plan, fmt.Errorf("new event block changed cursor frame %d", frames[i].meta.cursor)
		}
	}
	plan.encoded = encoded
	plan.pack = int64(len(encoded)+16*len(frames)+160) < oldBytes
	return plan, nil
}

func (s *Store) applyRepackPlan(ctx context.Context, hash [32]byte, checkpoint repackCheckpoint, plan repackPlan, through uint64) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout='250ms'`); err != nil {
			return err
		}
		if _, err := lockRow(ctx, tx, checkpoint.SessionID); err != nil {
			var pgErr *pq.Error
			if errors.As(err, &pgErr) && pgErr.Code == "55P03" {
				return fmt.Errorf("%w: %w", ErrCompactionSessionBusy, err)
			}
			return err
		}
		cursors := make([]int64, len(plan.frames))
		for i, frame := range plan.frames {
			cursors[i] = int64(frame.meta.cursor)
		}
		rows, err := tx.QueryContext(ctx, `SELECT cursor,kind,payload FROM session_event
			WHERE session_id=$1 AND cursor=ANY($2) ORDER BY cursor`, checkpoint.SessionID, pq.Array(cursors))
		if err != nil {
			return err
		}
		index := 0
		for rows.Next() {
			var cursor uint64
			var kind historypb.EventKind
			var payload []byte
			if err := rows.Scan(&cursor, &kind, &payload); err != nil {
				rows.Close()
				return err
			}
			if index >= len(plan.frames) || cursor != plan.frames[index].meta.cursor || kind != checkpoint.Kind ||
				len(payload) != plan.frames[index].meta.size || plan.frames[index].payload != nil && !bytes.Equal(payload, plan.frames[index].payload) {
				rows.Close()
				return errStaleRepackCandidate
			}
			index++
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		if index != len(plan.frames) {
			return errStaleRepackCandidate
		}
		var removed bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(
			SELECT FROM session_event_removed_range r WHERE r.session_id=$1 AND EXISTS
				(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor)
			UNION ALL
			SELECT FROM session_event_settled_range r WHERE r.session_id=$1 AND EXISTS
				(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor))`,
			checkpoint.SessionID, pq.Array(cursors)).Scan(&removed); err != nil {
			return err
		}
		if removed {
			return errStaleRepackCandidate
		}
		if plan.pack {
			values, sizes := make([]int64, len(plan.frames)), make([]int64, len(plan.frames))
			for i, frame := range plan.frames {
				values[i], sizes[i] = int64(frame.meta.cursor), int64(frame.meta.indexSize)
			}
			first, last := values[0], values[len(values)-1]
			if _, err := tx.ExecContext(ctx, `INSERT INTO session_event_block
				(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,$2,$3,$4,$5,$6,$7)`,
				checkpoint.SessionID, first, last, checkpoint.Kind, pq.Array(values), pq.Array(sizes), plan.encoded); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, `DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2)`, checkpoint.SessionID, pq.Array(cursors)); err != nil {
				return err
			}
		} else if _, err := tx.ExecContext(ctx, `UPDATE session_event SET compactable=false WHERE session_id=$1 AND cursor=ANY($2)`,
			checkpoint.SessionID, pq.Array(cursors)); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE history_event_repack_checkpoint SET pack_after=GREATEST(pack_after,$3),updated_at=now()
			WHERE profile_hash=$1 AND session_id=$2`, hash[:], checkpoint.SessionID, through)
		return err
	})
}
