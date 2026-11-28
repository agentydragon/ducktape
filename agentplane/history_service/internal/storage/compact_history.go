package storage

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
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
	// ErrCompactionBatchBudget marks a repack transaction that exceeded its configured time budget.
	ErrCompactionBatchBudget = errors.New("history repack batch transaction budget exceeded")
	errStaleRepackCandidate  = errors.New("history repack candidate changed")
)

const (
	// DefaultRepackBlocksPerTxn caps the number of packer cuts in one packing transaction.
	DefaultRepackBlocksPerTxn = 4
	// DefaultRepackBatchBudget bounds each append-lock transaction, including row-lock wait.
	DefaultRepackBatchBudget       = 5 * time.Second
	DefaultRepackLockWait          = 250 * time.Millisecond
	repackBatchMaxAttempts         = 3
	repackUnpackPhase        int16 = 0
	repackPackPhase          int16 = 1
	repackSessionPage              = 100
)

// CompactionResult reports logical work and stored payload bytes, excluding table and index pages.
type CompactionResult struct {
	Entries, Blocks, SourceBlocks, LegacyBlocks, VersionedSourceBlocks int64
	UncompressedBytes, PackedBytes, Skipped                            int64
}

// RepackBeforeBatch is called after a bounded candidate is prepared and before its short
// append-lock transaction begins. It is used for operator-controlled pacing and pause guards.
type RepackBeforeBatch func(context.Context, int64) error

type repackSessionCursor struct {
	SessionID   uuid.UUID
	Through     uint64
	Kind        historypb.EventKind
	Phase       int16
	UnpackAfter uint64
	PackAfter   uint64
	Complete    bool
}

type repackRun struct {
	OpeningPosition uint64
	AfterPosition   uint64
}

type deferredRepackSession struct {
	cursor repackSessionCursor
	err    error
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

type repackPlan struct {
	frames  []repackRawFrame
	encoded []byte
	pack    bool
}

func (s *Store) repackKinds() ([]historypb.EventKind, error) {
	var kinds []historypb.EventKind
	for n := range historypb.EventKind_name {
		if n != 0 {
			kinds = append(kinds, historypb.EventKind(n))
		}
	}
	slices.Sort(kinds)
	for _, kind := range kinds {
		if s.options.EventPackers[kind] == nil {
			return nil, fmt.Errorf("event block policy missing for History kind %s", kind)
		}
	}
	return kinds, nil
}

// RepackSession repacks one canary Session using in-memory cursors. If the process stops,
// the next invocation safely scans its current history again.
func (s *Store) RepackSession(ctx context.Context, sessionID uuid.UUID, apply bool, before RepackBeforeBatch) (CompactionResult, error) {
	kinds, err := s.repackKinds()
	if err != nil {
		return CompactionResult{}, err
	}
	if !apply {
		return s.previewHistorySession(ctx, sessionID, kinds, before)
	}
	cursor, err := s.openRepackCursor(ctx, sessionID, kinds)
	if err != nil {
		return CompactionResult{}, err
	}
	return s.runRepackSession(ctx, &cursor, kinds, before)
}

// RepackAll enumerates session_history directly, so inactive Sessions remain in scope. The
// opening recording position and each Session's cursor watermark live only for this process.
func (s *Store) RepackAll(ctx context.Context, apply bool, before RepackBeforeBatch) (CompactionResult, error) {
	kinds, err := s.repackKinds()
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
	var run repackRun
	if err := s.db.QueryRowContext(ctx, `SELECT COALESCE(max(recorded_position),0) FROM session_history`).
		Scan(&run.OpeningPosition); err != nil {
		return CompactionResult{}, err
	}
	var total CompactionResult
	var deferred []deferredRepackSession
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
			run.AfterPosition = position
			cursor, err := s.openRepackCursor(ctx, session.SessionID, kinds)
			if errors.Is(err, sql.ErrNoRows) {
				continue
			}
			if err != nil {
				return total, err
			}
			result, err := s.runRepackSession(ctx, &cursor, kinds, before)
			addCompactionResult(&total, result)
			if errors.Is(err, ErrCompactionSessionBusy) || errors.Is(err, ErrCompactionBatchBudget) {
				deferred = append(deferred, deferredRepackSession{cursor: cursor, err: err})
				continue
			}
			if err != nil {
				return total, fmt.Errorf("repacking Session %s: %w", session.SessionID, err)
			}
		}
	}
	// Busy sessions and exhausted batches stay in memory while the run scans the rest, then
	// receive bounded retries. If the process exits, a new run starts its safe scan from the start.
	for attempt := 0; attempt < 3 && len(deferred) > 0; attempt++ {
		next := deferred[:0]
		for _, pending := range deferred {
			result, err := s.runRepackSession(ctx, &pending.cursor, kinds, before)
			addCompactionResult(&total, result)
			if errors.Is(err, ErrCompactionSessionBusy) || errors.Is(err, ErrCompactionBatchBudget) {
				pending.err = err
				next = append(next, pending)
				continue
			}
			if err != nil {
				return total, fmt.Errorf("retrying repack for Session %s: %w", pending.cursor.SessionID, err)
			}
		}
		deferred = next
		if attempt < 2 && len(deferred) > 0 {
			timer := time.NewTimer(time.Duration(attempt+1) * 250 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return total, ctx.Err()
			case <-timer.C:
			}
		}
	}
	if len(deferred) > 0 {
		for _, pending := range deferred {
			if errors.Is(pending.err, ErrCompactionBatchBudget) {
				return total, fmt.Errorf("%w: %d Sessions remain deferred for a later run", ErrCompactionBatchBudget, len(deferred))
			}
		}
		return total, fmt.Errorf("%w: %d Sessions remain deferred for a later run", ErrCompactionSessionBusy, len(deferred))
	}
	return total, nil
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

func (s *Store) openRepackCursor(ctx context.Context, sessionID uuid.UUID, kinds []historypb.EventKind) (repackSessionCursor, error) {
	var checkpoint repackSessionCursor
	checkpoint.SessionID, checkpoint.Kind, checkpoint.Phase = sessionID, kinds[0], repackUnpackPhase
	err := s.db.QueryRowContext(ctx, `SELECT last_cursor FROM session_history WHERE id=$1`, sessionID).Scan(&checkpoint.Through)
	return checkpoint, err
}

func (s *Store) runRepackSession(ctx context.Context, checkpoint *repackSessionCursor, kinds []historypb.EventKind, before RepackBeforeBatch) (CompactionResult, error) {
	var result CompactionResult
	for !checkpoint.Complete {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		if checkpoint.Phase == repackUnpackPhase {
			block, found, err := s.findRepackBlock(ctx, *checkpoint, checkpoint.UnpackAfter)
			if err != nil {
				return result, err
			}
			if !found {
				checkpoint.Phase = repackPackPhase
				continue
			}
			if before != nil {
				if err := before(ctx, int64(len(block.payload))); err != nil {
					return result, err
				}
			}
			err = s.unpackRepackBlock(ctx, *checkpoint, block)
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
		_, found, err := s.findRepackBlock(ctx, *checkpoint, checkpoint.PackAfter)
		if err != nil {
			return result, err
		}
		if found {
			checkpoint.UnpackAfter = min(checkpoint.UnpackAfter, checkpoint.PackAfter)
			checkpoint.Phase = repackUnpackPhase
			continue
		}
		metas, hasMore, err := s.nextRepackRawMeta(ctx, *checkpoint)
		if err != nil {
			return result, err
		}
		if len(metas) == 0 {
			if err := advanceRepackKind(checkpoint, kinds); err != nil {
				return result, err
			}
			continue
		}
		packer := s.options.EventPackers[checkpoint.Kind]
		sizes := make([]int, len(metas))
		for i, meta := range metas {
			sizes[i] = meta.size
		}
		// The metadata page includes one length-only lookahead at the byte
		// or entry limit. Never consume that lookahead in a later cut.
		available := len(metas)
		if hasMore && available > 1 {
			available--
		}
		var cuts []eventblock.Cut
		n := 0
		for n < available && len(cuts) < s.options.RepackBlocksPerTxn {
			cut := packer.NextSizes(sizes[n:], !hasMore)
			if cut.N == 0 || n+cut.N > available {
				break
			}
			n += cut.N
			cuts = append(cuts, cut)
		}
		if n == 0 {
			return result, errors.New("event block profile did not advance a bounded repack batch")
		}
		frames, err := s.loadRepackRawFrames(ctx, *checkpoint, metas[:n])
		if err != nil {
			if errors.Is(err, errStaleRepackCandidate) {
				continue
			}
			return result, err
		}
		plans := make([]repackPlan, 0, len(cuts))
		offset := 0
		for _, cut := range cuts {
			plan, err := buildRepackPlan(packer, frames[offset:offset+cut.N], cut)
			if err != nil {
				return result, err
			}
			plans = append(plans, plan)
			offset += cut.N
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
		err = s.applyRepackPlans(ctx, *checkpoint, plans, frames)
		if errors.Is(err, errStaleRepackCandidate) {
			continue
		}
		if err != nil {
			return result, err
		}
		result.Entries += int64(len(frames))
		result.UncompressedBytes += sourceBytes
		for _, plan := range plans {
			if plan.pack {
				result.Blocks++
				result.PackedBytes += int64(len(plan.encoded))
			} else {
				result.Skipped += int64(len(plan.frames))
			}
		}
		checkpoint.PackAfter = last
	}
	return result, nil
}

func (s *Store) findRepackBlock(ctx context.Context, checkpoint repackSessionCursor, after uint64) (storedBlock, bool, error) {
	block, err := scanStoredBlock(s.db.QueryRowContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
		FROM session_event_block WHERE session_id=$1 AND kind=$2 AND first_cursor<=$3 AND last_cursor>$4
		ORDER BY first_cursor LIMIT 1`, checkpoint.SessionID, checkpoint.Kind, checkpoint.Through, after))
	if errors.Is(err, sql.ErrNoRows) {
		return storedBlock{}, false, nil
	}
	if err != nil {
		return storedBlock{}, false, err
	}
	return block, true, nil
}

func advanceRepackKind(checkpoint *repackSessionCursor, kinds []historypb.EventKind) error {
	index := slices.Index(kinds, checkpoint.Kind)
	if index < 0 {
		return fmt.Errorf("History repack cursor has unknown kind %d", checkpoint.Kind)
	}
	if index+1 == len(kinds) {
		checkpoint.Complete = true
		return nil
	}
	next := kinds[index+1]
	checkpoint.Kind, checkpoint.Phase = next, repackUnpackPhase
	checkpoint.UnpackAfter, checkpoint.PackAfter = 0, 0
	return nil
}

func (s *Store) retryRepackBatch(ctx context.Context, sessionID uuid.UUID, operation string, run func() error) error {
	for attempt := 1; ; attempt++ {
		err := run()
		if !errors.Is(err, ErrCompactionBatchBudget) || attempt == repackBatchMaxAttempts {
			return err
		}
		slog.WarnContext(ctx, "History repack batch retry after transaction budget",
			"session_id", sessionID, "operation", operation, "attempt", attempt,
			"budget", s.options.RepackBatchBudget)
		timer := time.NewTimer(time.Duration(attempt) * 50 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

func (s *Store) runRepackBatchTransaction(ctx context.Context, sessionID uuid.UUID, operation string, work func(context.Context, *sql.Tx) error) error {
	if err := postgres.ValidateMaintenanceLockDuration(s.options.RepackLockWait); err != nil {
		return err
	}
	budget := s.options.RepackBatchBudget
	contextBudget := budget * 9 / 10
	if contextBudget <= 0 {
		contextBudget = budget
	}
	batchCtx, cancel := context.WithTimeout(ctx, contextBudget)
	defer cancel()
	var lockWait, lockHold time.Duration
	var lockAcquiredAt time.Time
	err := postgres.NamedTransaction(batchCtx, s.db, sql.TxOptions{}, "history.repack_"+operation, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(batchCtx, `SELECT set_config('lock_timeout',$1,true),set_config('transaction_timeout',$2,true)`,
			fmt.Sprintf("%dms", s.options.RepackLockWait.Milliseconds()), budget.String()); err != nil {
			return err
		}
		waitStarted := time.Now()
		if _, err := lockRow(batchCtx, tx, sessionID); err != nil {
			lockWait = time.Since(waitStarted)
			var pgErr *pq.Error
			if errors.As(err, &pgErr) && pgErr.Code == "55P03" {
				return fmt.Errorf("%w: %w", ErrCompactionSessionBusy, err)
			}
			return err
		}
		lockWait = time.Since(waitStarted)
		lockAcquiredAt = time.Now()
		return work(batchCtx, tx)
	})
	if !lockAcquiredAt.IsZero() {
		lockHold = time.Since(lockAcquiredAt)
	}
	if err != nil && errors.Is(batchCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil {
		err = fmt.Errorf("%w: %s transaction exceeded %s and rolled back: %v", ErrCompactionBatchBudget, operation, budget, err)
	}
	outcome := "committed"
	switch {
	case errors.Is(err, ErrCompactionBatchBudget):
		outcome = "budget_exceeded"
	case errors.Is(err, ErrCompactionSessionBusy):
		outcome = "lock_timeout"
	case errors.Is(err, errStaleRepackCandidate):
		outcome = "stale_candidate"
	case err != nil:
		outcome = "rolled_back"
	}
	slog.InfoContext(ctx, "History repack batch transaction",
		"session_id", sessionID, "operation", operation,
		"lock_wait", lockWait, "lock_wait_budget", s.options.RepackLockWait,
		"lock_hold", lockHold, "context_budget", contextBudget, "budget", budget, "outcome", outcome)
	return err
}

func (s *Store) unpackRepackBlock(ctx context.Context, checkpoint repackSessionCursor, block storedBlock) error {
	return s.retryRepackBatch(ctx, checkpoint.SessionID, "unpack", func() error {
		return s.runRepackBatchTransaction(ctx, checkpoint.SessionID, "unpack", func(txCtx context.Context, tx *sql.Tx) error {
			var current storedBlock
			err := tx.QueryRowContext(txCtx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload
				FROM session_event_block WHERE session_id=$1 AND first_cursor=$2`, checkpoint.SessionID, block.first).
				Scan(&current.first, &current.last, &current.kind, &current.cursors, &current.sizes, &current.payload)
			if errors.Is(err, sql.ErrNoRows) {
				return errStaleRepackCandidate
			}
			if err != nil {
				return err
			}
			if !sameStoredBlock(current, block) {
				return errStaleRepackCandidate
			}
			var removed bool
			if err := tx.QueryRowContext(txCtx, `SELECT EXISTS(
				SELECT FROM session_event_removed_range r WHERE r.session_id=$1 AND EXISTS
					(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor)
				UNION ALL
				SELECT FROM session_event_settled_range r WHERE r.session_id=$1 AND EXISTS
					(SELECT FROM unnest($2::bigint[]) c WHERE c BETWEEN r.first_cursor AND r.last_cursor))`,
				checkpoint.SessionID, block.cursors).Scan(&removed); err != nil {
				return err
			}
			if removed {
				return fmt.Errorf("event block %d still contains a removed or settled cursor", block.first)
			}
			if _, err := tx.ExecContext(txCtx, `DELETE FROM session_event_block WHERE session_id=$1 AND first_cursor=$2`, checkpoint.SessionID, block.first); err != nil {
				return err
			}
			for _, frame := range block.frames {
				if err := insertRawFrame(txCtx, tx, checkpoint.SessionID, frame); err != nil {
					return err
				}
			}
			return nil
		})
	})
}

func sameStoredBlock(a, b storedBlock) bool {
	return a.first == b.first && a.last == b.last && a.kind == b.kind && slices.Equal(a.cursors, b.cursors) &&
		slices.Equal(a.sizes, b.sizes) && bytes.Equal(a.payload, b.payload)
}

func (s *Store) nextRepackRawMeta(ctx context.Context, checkpoint repackSessionCursor) ([]repackRawMeta, bool, error) {
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
	return metas, more, nil
}

func (s *Store) loadRepackRawFrames(ctx context.Context, checkpoint repackSessionCursor, metas []repackRawMeta) ([]repackRawFrame, error) {
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
	plan.pack = estimatedBlockBytes(encoded, len(frames)) < oldBytes
	return plan, nil
}

func (s *Store) applyRepackPlans(ctx context.Context, checkpoint repackSessionCursor, plans []repackPlan, frames []repackRawFrame) error {
	return s.retryRepackBatch(ctx, checkpoint.SessionID, "pack", func() error {
		return s.runRepackBatchTransaction(ctx, checkpoint.SessionID, "pack", func(txCtx context.Context, tx *sql.Tx) error {
			cursors := make([]int64, len(frames))
			for i, frame := range frames {
				cursors[i] = int64(frame.meta.cursor)
			}
			rows, err := tx.QueryContext(txCtx, `SELECT cursor,kind,payload FROM session_event
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
				if index >= len(frames) || cursor != frames[index].meta.cursor || kind != checkpoint.Kind ||
					len(payload) != frames[index].meta.size || frames[index].payload != nil && !bytes.Equal(payload, frames[index].payload) {
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
			if index != len(frames) {
				return errStaleRepackCandidate
			}
			var removed bool
			if err := tx.QueryRowContext(txCtx, `SELECT EXISTS(
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
			var packed, skipped []int64
			for _, plan := range plans {
				if !plan.pack {
					for _, frame := range plan.frames {
						skipped = append(skipped, int64(frame.meta.cursor))
					}
					continue
				}

				values, sizes := make([]int64, len(plan.frames)), make([]int64, len(plan.frames))
				for i, frame := range plan.frames {
					values[i], sizes[i] = int64(frame.meta.cursor), int64(frame.meta.indexSize)
				}
				first, last := values[0], values[len(values)-1]
				if _, err := tx.ExecContext(txCtx, `INSERT INTO session_event_block
				(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,$2,$3,$4,$5,$6,$7)`,
					checkpoint.SessionID, first, last, checkpoint.Kind, pq.Array(values), pq.Array(sizes), plan.encoded); err != nil {
					return err
				}
				packed = append(packed, values...)
			}
			if len(packed) > 0 {
				if _, err := tx.ExecContext(txCtx, `DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2)`, checkpoint.SessionID, pq.Array(packed)); err != nil {
					return err
				}
			}
			if len(skipped) > 0 {
				if _, err := tx.ExecContext(txCtx, `UPDATE session_event SET compactable=false WHERE session_id=$1 AND cursor=ANY($2)`, checkpoint.SessionID, pq.Array(skipped)); err != nil {
					return err
				}
			}
			return nil
		})
	})
}
