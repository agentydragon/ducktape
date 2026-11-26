package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/lib/pq"
)

const (
	journalRepackBatchEntries = eventblock.MaxEntries
	journalRepackBatchBytes   = eventblock.MaxBytes
	journalRepackStoredBytes  = eventblock.MaxBytes + (64 << 10)
	journalLockWait           = 100 * time.Millisecond
	journalLockHold           = 250 * time.Millisecond
)

var (
	ErrJournalBusy         = errors.New("journal Session is busy")
	ErrJournalBatchChanged = errors.New("journal input changed during preparation")
)

// JournalCompactionOptions controls pacing and bounded maintenance runs.
type JournalCompactionOptions struct {
	BatchDelay        time.Duration
	PauseFile         string
	ContentionRetries int
	RetryDelay        time.Duration
}

// JournalCompactionResult records the fixed opening watermark and the work completed
// under the selected profile. Lock timings include each short batch transaction.
type JournalCompactionResult struct {
	Events, Blocks, StoredBytes, PackedBytes int64
	OpeningCursor                            uint64
	Batches                                  int64
	Completed                                bool
	LockHoldTotal, LockHoldMax               time.Duration
}

type journalRepackState struct {
	opening uint64
	through uint64
}

type repackStorage struct {
	first, last uint64
	block       bool
	payload     []byte
}

type repackEntry struct {
	sequence       uint64
	frame          []byte
	standaloneWire []byte
	storedCost     int
	packable       bool
}

type repackPlan struct {
	sources []repackStorage
	outputs []repackStorage
	state   journalRepackState
	result  JournalCompactionResult
}

// ListJournalSessions lists all retained Sessions in keyset order, including inactive Sessions.
func (s *Store) ListJournalSessions(ctx context.Context, after string, limit int) ([]string, error) {
	if limit <= 0 || limit > 4096 {
		return nil, protocol.ErrInvalid
	}
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	rows, err := s.db.QueryContext(ctx, `SELECT id FROM sampler_session WHERE id>$1 ORDER BY id LIMIT $2`, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// CompactJournal retains the default paced behavior for in-process callers.
func (s *Store) CompactJournal(ctx context.Context, sessionID string, apply bool) (JournalCompactionResult, error) {
	return s.CompactJournalWithOptions(ctx, sessionID, apply, JournalCompactionOptions{
		BatchDelay: 50 * time.Millisecond, ContentionRetries: 3, RetryDelay: 250 * time.Millisecond,
	})
}

// CompactJournalWithOptions captures one fixed cursor watermark and repacks it in
// batches of at most 256 entries and 8 MiB. Compression runs without the Session
// lock. Apply compares exact source keys and payloads, then atomically replaces a
// batch. Progress stays in memory; a later invocation safely scans from the start.
func (s *Store) CompactJournalWithOptions(ctx context.Context, sessionID string, apply bool, options JournalCompactionOptions) (JournalCompactionResult, error) {
	var result JournalCompactionResult
	if options.BatchDelay < 0 || options.BatchDelay > time.Minute ||
		options.ContentionRetries < 0 || options.ContentionRetries > 10 || options.RetryDelay < 0 || options.RetryDelay > time.Minute {
		return result, protocol.ErrInvalid
	}
	if _, err := eventblock.NewPacker(s.eventBlockProfile); err != nil {
		return result, err
	}
	opening, err := s.CurrentCursor(ctx, sessionID)
	if err != nil {
		return result, err
	}
	state := journalRepackState{opening: opening}
	result.OpeningCursor = state.opening

	lastProgress := time.Now()
	for {
		if err := waitForJournalPause(ctx, options.PauseFile); err != nil {
			return result, err
		}
		for attempt := 0; ; attempt++ {
			plan, err := s.prepareJournalRepack(ctx, sessionID, state)
			if err != nil {
				return result, err
			}
			if apply {
				hold, err := s.applyJournalRepack(ctx, sessionID, state, plan)
				addLockHold(&result, hold)
				if errors.Is(err, ErrJournalBatchChanged) || errors.Is(err, ErrJournalBusy) {
					if attempt < options.ContentionRetries {
						if err := waitForJournalCompaction(ctx, options.RetryDelay); err != nil {
							return result, err
						}
						continue
					}
					return result, fmt.Errorf("%w after %d compare-and-swap retries", ErrJournalBusy, attempt)
				}
				if err != nil {
					return result, err
				}
			}
			state = plan.state
			result.Events += plan.result.Events
			result.Blocks += plan.result.Blocks
			result.StoredBytes += plan.result.StoredBytes
			result.PackedBytes += plan.result.PackedBytes
			result.Batches++
			break
		}
		if state.through >= state.opening {
			result.Completed = true
			return result, nil
		}
		if now := time.Now(); now.Sub(lastProgress) >= 30*time.Second {
			slog.InfoContext(ctx, "sampler journal repack progress", "session_id", sessionID, "apply", apply,
				"cursor", state.through, "through", state.opening, "events", result.Events,
				"blocks", result.Blocks, "stored_bytes", result.StoredBytes, "packed_bytes", result.PackedBytes,
				"lock_hold_total_ms", result.LockHoldTotal.Milliseconds(), "lock_hold_max_ms", result.LockHoldMax.Milliseconds())
			lastProgress = now
		}
		if options.BatchDelay > 0 {
			if err := waitForJournalCompaction(ctx, options.BatchDelay); err != nil {
				return result, err
			}
		}
	}
}

func addLockHold(result *JournalCompactionResult, hold time.Duration) {
	if hold <= 0 {
		return
	}
	result.LockHoldTotal += hold
	if hold > result.LockHoldMax {
		result.LockHoldMax = hold
	}
}

func waitForJournalCompaction(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func waitForJournalPause(ctx context.Context, path string) error {
	if path == "" {
		return nil
	}
	paused := false
	for {
		_, err := os.Stat(path)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("check repack pause file: %w", err)
		}
		if !paused {
			slog.InfoContext(ctx, "sampler journal repack paused", "pause_file", path)
			paused = true
		}
		if err := waitForJournalCompaction(ctx, time.Second); err != nil {
			return err
		}
	}
}

func (s *Store) prepareJournalRepack(ctx context.Context, sessionID string, state journalRepackState) (repackPlan, error) {
	var sources []repackStorage
	hasMore := false
	err := postgres.ReadTransaction(ctx, s.db, sql.TxOptions{ReadOnly: true}, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `
			WITH storage AS (
				SELECT sequence AS first_sequence,sequence AS last_sequence,entry AS payload,FALSE AS is_block,octet_length(entry) AS stored_bytes
				FROM sampler_event WHERE session_id=$1 AND sequence>$2 AND sequence<=$3
				UNION ALL
				SELECT first_sequence,last_sequence,payload,TRUE,octet_length(payload)
				FROM sampler_event_block WHERE session_id=$1 AND first_sequence<=$3 AND last_sequence>$2
			), limited AS (
				SELECT * FROM storage ORDER BY first_sequence LIMIT $5
			), ordered AS (
				SELECT *,row_number() OVER (ORDER BY first_sequence) AS ordinal,
					sum(stored_bytes) OVER (ORDER BY first_sequence ROWS UNBOUNDED PRECEDING) AS cumulative_bytes
				FROM limited
			)
			SELECT first_sequence,last_sequence,
				CASE WHEN cumulative_bytes<=$4 THEN payload END,is_block,stored_bytes
			FROM ordered
			WHERE ordinal<=$5 AND (cumulative_bytes<=$4 OR ordinal=1 OR cumulative_bytes-stored_bytes<$4)
			ORDER BY first_sequence`, sessionID, state.through, state.opening, journalRepackStoredBytes, journalRepackBatchEntries+1)
		if err != nil {
			return err
		}
		defer rows.Close()
		var previous uint64
		for rows.Next() {
			var source repackStorage
			var storedBytes int
			if err := rows.Scan(&source.first, &source.last, &source.payload, &source.block, &storedBytes); err != nil {
				return err
			}
			if len(sources) >= journalRepackBatchEntries {
				hasMore = true
				break
			}
			if source.first <= state.through && (!source.block || source.last <= state.through) ||
				source.last < source.first || (previous != 0 && source.first <= previous) {
				return fmt.Errorf("overlapping or invalid journal storage at cursor %d", source.first)
			}
			count := source.last - source.first + 1
			if count > eventblock.MaxEntries {
				return fmt.Errorf("journal storage span %d–%d exceeds codec limit", source.first, source.last)
			}
			if storedBytes > journalRepackStoredBytes || source.payload == nil {
				if len(sources) == 0 {
					return fmt.Errorf("unsupported journal payload of %d stored bytes at cursor %d", storedBytes, source.first)
				}
				hasMore = true
				break
			}
			sources = append(sources, source)
			previous = source.last
		}
		return rows.Err()
	})
	if err != nil {
		return repackPlan{}, err
	}

	var entries []repackEntry
	decodedBytes := 0
	for i, source := range sources {
		count := int(source.last - source.first + 1)
		if len(entries)+count > journalRepackBatchEntries {
			sources = sources[:i]
			hasMore = true
			break
		}
		decoded, err := decodeRepackSource(source)
		if err != nil {
			return repackPlan{}, err
		}
		sourceDecodedBytes := 0
		for _, entry := range decoded {
			sourceDecodedBytes += len(entry.frame)
		}
		if len(entries) > 0 && decodedBytes+sourceDecodedBytes > journalRepackBatchBytes {
			sources = sources[:i]
			hasMore = true
			break
		}
		if sourceDecodedBytes > journalRepackBatchBytes {
			return repackPlan{}, fmt.Errorf("decoded journal source %d–%d exceeds batch byte limit", source.first, source.last)
		}
		baseCost, extraCost := len(source.payload)/len(decoded), len(source.payload)%len(decoded)
		for j := range decoded {
			entry := decoded[j]
			entry.sequence = source.first + uint64(j)
			entry.storedCost = baseCost
			if j < extraCost {
				entry.storedCost++
			}
			if entry.packable {
				if _, err := decodeJournalEvent(entry.frame, entry.sequence); err != nil {
					return repackPlan{}, err
				}
			}
			entries = append(entries, entry)
		}
		decodedBytes += sourceDecodedBytes
	}

	if len(sources) == 0 {
		return repackPlan{state: journalRepackState{opening: state.opening, through: state.opening}}, nil
	}
	outputs, err := repackJournalEntries(entries, hasMore, state.opening, s.eventBlockProfile)
	if err != nil {
		return repackPlan{}, err
	}
	if err := validateJournalRepackCoverage(entries, outputs); err != nil {
		return repackPlan{}, err
	}
	// Every selected source is covered, including an unsealed tail returned as
	// standalone rows. The final page may end in a gap before the opening cursor.
	through := state.opening
	if hasMore {
		through = min(sources[len(sources)-1].last, state.opening)
	}
	plan := repackPlan{
		sources: sources, outputs: outputs,
		state: journalRepackState{opening: state.opening, through: through},
	}
	plan.result.Events = int64(len(entries))
	for _, source := range sources {
		plan.result.StoredBytes += int64(len(source.payload))
	}
	for _, output := range outputs {
		if output.block {
			plan.result.Blocks++
			plan.result.PackedBytes += int64(len(output.payload))
		}
	}
	return plan, nil
}

func decodeRepackSource(source repackStorage) ([]repackEntry, error) {
	if !source.block {
		if blockPayloadInEventRow(source.payload) {
			return nil, fmt.Errorf("block-compressed payload in standalone event at cursor %d", source.first)
		}
		if len(source.payload) == 0 {
			return nil, fmt.Errorf("empty standalone event at cursor %d", source.first)
		}
		frame, err := eventcodec.UnpackPackableEntry(source.payload)
		if err != nil {
			return nil, fmt.Errorf("unpack event at cursor %d for repack: %w", source.first, err)
		}
		entry := repackEntry{frame: frame, packable: len(frame) > 0, standaloneWire: bytes.Clone(source.payload)}
		return []repackEntry{entry}, nil
	}
	frames, _, err := decodeJournalBlock(source.payload, source.first, source.last)
	if err != nil {
		return nil, err
	}
	entries := make([]repackEntry, len(frames))
	for i, frame := range frames {
		if len(frame) == 0 {
			return nil, fmt.Errorf("empty frame at cursor %d", source.first+uint64(i))
		}
		entries[i] = repackEntry{frame: frame, packable: true}
	}
	return entries, nil
}

func repackJournalEntries(entries []repackEntry, hasMore bool, opening uint64, profile eventblock.Profile) ([]repackStorage, error) {
	packer, err := eventblock.NewPacker(profile)
	if err != nil {
		return nil, err
	}
	var outputs []repackStorage
	for start := 0; start < len(entries); {
		if !entries[start].packable {
			entry := entries[start]
			outputs = append(outputs, repackStorage{
				first: entry.sequence, last: entry.sequence,
				payload: bytes.Clone(entry.standaloneWire),
			})
			start++
			continue
		}
		end := start + 1
		for end < len(entries) && entries[end].packable && entries[end].sequence == entries[end-1].sequence+1 && entries[end-1].sequence < opening {
			end++
		}
		flushTail := end < len(entries) || !hasMore
		stream := entries[start:end]
		frames := make([][]byte, len(stream))
		for i := range stream {
			frames[i] = stream[i].frame
		}
		for offset := 0; offset < len(stream); {
			cut := packer.Next(frames[offset:], flushTail)
			count := cut.N
			switch cut.Decision {
			case eventblock.NeedMore:
				// Keep the bounded, unsealed tail as standalone rows.
				count = len(stream) - offset
			case eventblock.Standalone:
				// The frame exceeds the block size limit.
			case eventblock.Seal:
				selected := stream[offset : offset+count]
				payload, err := packer.Encode(frames[offset : offset+count])
				if err != nil {
					return nil, err
				}
				baseline := 0
				for _, entry := range selected {
					baseline += entry.storedCost
				}
				if len(payload) <= baseline {
					outputs = append(outputs, repackStorage{
						first: selected[0].sequence, last: selected[count-1].sequence,
						block: true, payload: payload,
					})
					offset += count
					continue
				}
			default:
				return nil, fmt.Errorf("unknown event block decision %d", cut.Decision)
			}
			for _, entry := range stream[offset : offset+count] {
				payload, err := standaloneJournalPayload(entry)
				if err != nil {
					return nil, err
				}
				outputs = append(outputs, repackStorage{first: entry.sequence, last: entry.sequence, payload: payload})
			}
			offset += count
		}
		start = end
	}
	return outputs, nil
}

func standaloneJournalPayload(entry repackEntry) ([]byte, error) {
	if len(entry.standaloneWire) > 0 {
		return bytes.Clone(entry.standaloneWire), nil
	}
	return eventcodec.Encode(entry.frame)
}

func validateJournalRepackCoverage(entries []repackEntry, outputs []repackStorage) error {
	entryIndex := 0
	for _, output := range outputs {
		if output.first > output.last {
			return fmt.Errorf("repack output has reversed cursor range %d–%d", output.first, output.last)
		}
		count := int(output.last - output.first + 1)
		if output.block {
			decoded, err := eventblock.Decode(output.payload)
			if err != nil {
				return fmt.Errorf("verify output block %d–%d: %w", output.first, output.last, err)
			}
			if len(decoded) != count {
				return fmt.Errorf("output block %d–%d contains %d frames, want %d", output.first, output.last, len(decoded), count)
			}
			for i := range decoded {
				if entryIndex >= len(entries) || entries[entryIndex].sequence != output.first+uint64(i) ||
					!entries[entryIndex].packable || !bytes.Equal(decoded[i], entries[entryIndex].frame) {
					return fmt.Errorf("output block %d–%d does not preserve ordered source frame %d", output.first, output.last, output.first+uint64(i))
				}
				entryIndex++
			}
			continue
		}
		if count != 1 || entryIndex >= len(entries) || entries[entryIndex].sequence != output.first {
			return fmt.Errorf("standalone output at cursor %d does not match its source", output.first)
		}
		entry := entries[entryIndex]
		if entry.packable {
			frame, err := eventcodec.UnpackPackableEntry(output.payload)
			if err != nil || len(frame) == 0 || !bytes.Equal(frame, entry.frame) {
				return fmt.Errorf("standalone output at cursor %d does not preserve its source frame", output.first)
			}
		} else if !bytes.Equal(output.payload, entry.standaloneWire) {
			return fmt.Errorf("unpackable standalone output at cursor %d changed its stored bytes", output.first)
		}
		entryIndex++
	}
	if entryIndex != len(entries) {
		return fmt.Errorf("repack output covered %d of %d source entries", entryIndex, len(entries))
	}
	return nil
}

func (s *Store) applyJournalRepack(ctx context.Context, sessionID string, previous journalRepackState, plan repackPlan) (time.Duration, error) {
	return s.withJournalSessionLock(ctx, sessionID, func(tx *sql.Tx, cursor uint64) error {
		if cursor < previous.opening {
			return errors.New("sampler Session cursor regressed below repack opening watermark")
		}
		if err := compareJournalSources(ctx, tx, sessionID, previous, plan.sources); err != nil {
			return err
		}
		if slices.EqualFunc(plan.sources, plan.outputs, repackStorage.equal) {
			return nil
		}
		return replaceJournalStorage(ctx, tx, sessionID, plan.sources, plan.outputs)
	})
}

// Batch verification and replacement use the same inclusive cursor range.
const (
	repackEventRange = `session_id=$1 AND sequence BETWEEN $2 AND $3`
	repackBlockRange = `session_id=$1 AND first_sequence<=$3 AND last_sequence>=$2`
)

func compareJournalSources(ctx context.Context, tx *sql.Tx, sessionID string, state journalRepackState, expected []repackStorage) error {
	first, last := state.through+1, state.opening
	if len(expected) > 0 {
		first, last = expected[0].first, expected[len(expected)-1].last
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT sequence,sequence,entry,FALSE FROM sampler_event
		WHERE `+repackEventRange+`
		UNION ALL
		SELECT first_sequence,last_sequence,payload,TRUE FROM sampler_event_block
		WHERE `+repackBlockRange+`
		ORDER BY 1`, sessionID, first, last)
	if err != nil {
		return err
	}
	defer rows.Close()
	index := 0
	for rows.Next() {
		var actual repackStorage
		if err := rows.Scan(&actual.first, &actual.last, &actual.payload, &actual.block); err != nil {
			return err
		}
		if index >= len(expected) {
			return ErrJournalBatchChanged
		}
		if !actual.equal(expected[index]) {
			return ErrJournalBatchChanged
		}
		index++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if index != len(expected) {
		return ErrJournalBatchChanged
	}
	return nil
}

func (row repackStorage) equal(other repackStorage) bool {
	return row.first == other.first && row.last == other.last && row.block == other.block && bytes.Equal(row.payload, other.payload)
}

func replaceJournalStorage(ctx context.Context, tx *sql.Tx, sessionID string, sources []repackStorage, outputs []repackStorage) error {
	if len(sources) > 0 {
		first, last := sources[0].first, sources[len(sources)-1].last
		var rawCount, blockCount int64
		for _, source := range sources {
			if source.block {
				blockCount++
			} else {
				rawCount++
			}
		}
		deleted, err := tx.ExecContext(ctx, `DELETE FROM sampler_event WHERE `+repackEventRange, sessionID, first, last)
		if err != nil {
			return err
		}
		count, err := deleted.RowsAffected()
		if err != nil {
			return err
		}
		if count != rawCount {
			return fmt.Errorf("deleted %d standalone source rows, expected %d", count, rawCount)
		}
		deleted, err = tx.ExecContext(ctx, `DELETE FROM sampler_event_block WHERE `+repackBlockRange, sessionID, first, last)
		if err != nil {
			return err
		}
		count, err = deleted.RowsAffected()
		if err != nil {
			return err
		}
		if count != blockCount {
			return fmt.Errorf("deleted %d block source rows, expected %d", count, blockCount)
		}
	}
	if err := insertRepackOutputs(ctx, tx, sessionID, outputs, true); err != nil {
		return err
	}
	return insertRepackOutputs(ctx, tx, sessionID, outputs, false)
}

func insertRepackOutputs(ctx context.Context, tx *sql.Tx, sessionID string, outputs []repackStorage, blocks bool) error {
	var selected []repackStorage
	for _, output := range outputs {
		if output.block == blocks {
			selected = append(selected, output)
		}
	}
	if len(selected) == 0 {
		return nil
	}
	table, columns := "sampler_event", "session_id,sequence,entry"
	if blocks {
		table, columns = "sampler_event_block", "session_id,first_sequence,last_sequence,payload"
	}
	var query strings.Builder
	fmt.Fprintf(&query, "INSERT INTO %s(%s) VALUES ", table, columns)
	args := make([]any, 0, len(selected)*4)
	for i, output := range selected {
		if i > 0 {
			query.WriteByte(',')
		}
		base := len(args) + 1
		if blocks {
			fmt.Fprintf(&query, "($%d,$%d,$%d,$%d)", base, base+1, base+2, base+3)
			args = append(args, sessionID, output.first, output.last, output.payload)
		} else {
			if output.first != output.last {
				return fmt.Errorf("standalone output spans cursors %d–%d", output.first, output.last)
			}
			fmt.Fprintf(&query, "($%d,$%d,$%d)", base, base+1, base+2)
			args = append(args, sessionID, output.first, output.payload)
		}
	}
	result, err := tx.ExecContext(ctx, query.String(), args...)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != int64(len(selected)) {
		return fmt.Errorf("inserted %d repacked outputs, expected %d", count, len(selected))
	}
	return nil
}

func (s *Store) withJournalSessionLock(ctx context.Context, sessionID string, apply func(*sql.Tx, uint64) error) (time.Duration, error) {
	transactionCtx, cancel := context.WithTimeout(ctx, journalLockWait+journalLockHold)
	defer cancel()
	var holdTimer *time.Timer
	var lockStarted time.Time
	var holdExceeded atomic.Bool
	err := postgres.Transaction(transactionCtx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(transactionCtx, `SET LOCAL lock_timeout='100ms'`); err != nil {
			return err
		}
		var cursor int64
		err := tx.QueryRowContext(transactionCtx, `SELECT cursor FROM sampler_session WHERE id=$1 FOR UPDATE`, sessionID).Scan(&cursor)
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		lockStarted = time.Now()
		holdTimer = time.AfterFunc(journalLockHold, func() {
			holdExceeded.Store(true)
			cancel()
		})
		return apply(tx, uint64(cursor))
	})
	if holdTimer != nil {
		holdTimer.Stop()
	}
	var hold time.Duration
	if !lockStarted.IsZero() {
		hold = time.Since(lockStarted)
	}
	if holdExceeded.Load() {
		err = fmt.Errorf("%w: lock hold exceeded %s", ErrJournalBusy, journalLockHold)
	}
	var pgErr *pq.Error
	if errors.As(err, &pgErr) && pgErr.Code == "55P03" {
		err = fmt.Errorf("%w: session row lock timeout", ErrJournalBusy)
	}
	if err == nil && hold > journalLockHold {
		err = fmt.Errorf("%w: lock held for %s, limit %s", ErrJournalBusy, hold, journalLockHold)
	}
	return hold, err
}
