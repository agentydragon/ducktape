package journal

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"math"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	_ "modernc.org/sqlite"
)

const journalPageSize = 128

var (
	ErrCommandConflict   = errors.New("command ID reused for different work")
	ErrActiveTurnChanged = errors.New("target turn is no longer active")
	ErrFailed            = errors.New("journal failed; reopen it for recovery")
	ErrSealed            = errors.New("journal is sealed")
	ErrUnknownCommand    = errors.New("unknown command ID")
	ErrChangedOutside    = errors.New("journal changed outside its owner; reopen it for recovery")
)

type StorageError struct{ Err error }

func (e *StorageError) Error() string { return "journal transaction failed: " + e.Err.Error() }
func (e *StorageError) Unwrap() error { return e.Err }

type RecoveryState struct {
	ThroughCursor  uint64
	HarnessRunning bool
	ActiveTurnID   string
}

type SealState struct {
	Cursor         uint64
	IncarnationUID string
}

type AppendOptions struct {
	Sources            []uint64
	TerminalCommandIDs []string
}

type Journal struct {
	db       *sql.DB
	conn     *sql.Conn
	sourceID string

	mu             sync.Mutex
	state          RecoveryState
	seal           *SealState
	publishedState atomic.Pointer[RecoveryState]
	publishedSeal  atomic.Pointer[SealState]
	failure        error
	changed        chan struct{}
	closed         bool
}

type batchContextKey struct{}

type journalBatch struct {
	locked       bool
	journal      *Journal
	active       bool
	appended     []*eventlogpb.EventEntry
	sealOverride *SealState
	sealChanged  bool
	sequence     uint64
}

// Open applies pending schema migrations before recovering retained EventEntry rows.
func Open(ctx context.Context, path, sourceID string) (*Journal, error) {
	if sourceID == "" {
		return nil, errors.New("journal source ID is required")
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	for _, pragma := range []string{
		// DELETE-mode commits need an exclusive lock. Online backup readers may
		// briefly hold shared locks; wait before treating contention as a storage failure.
		"PRAGMA busy_timeout=5000",
		"PRAGMA journal_mode=DELETE",
		"PRAGMA synchronous=EXTRA",
		"PRAGMA foreign_keys=ON",
		"PRAGMA temp_store=FILE",
		"PRAGMA temp.cache_size=-2048",
	} {
		if _, err := db.ExecContext(ctx, pragma); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("configure journal SQLite connection: %w", err)
		}
	}
	if err := migrateJournalSchema(ctx, db); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("migrate runner journal schema: %w", err)
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	closeOnError := func(openErr error) (*Journal, error) {
		_ = conn.Close()
		_ = db.Close()
		return nil, openErr
	}
	for _, statement := range journalTemporarySchema {
		if _, err := conn.ExecContext(ctx, statement); err != nil {
			return closeOnError(fmt.Errorf("create runner journal temporary schema: %w", err))
		}
	}
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return closeOnError(fmt.Errorf("begin journal recovery: %w", err))
	}
	rollback := func(openErr error) (*Journal, error) {
		_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
		return closeOnError(openErr)
	}
	var lastCursor sql.NullInt64
	if err := conn.QueryRowContext(ctx, "SELECT MAX(cursor) FROM event_entry").Scan(&lastCursor); err != nil {
		return rollback(fmt.Errorf("read journal end: %w", err))
	}
	var (
		state            RecoveryState
		checkpointSource string
		through          int64
	)
	err = conn.QueryRowContext(
		ctx,
		"SELECT source_id, through_cursor, harness_running, active_turn_id FROM checkpoint WHERE id = 1",
	).Scan(&checkpointSource, &through, &state.HarnessRunning, &state.ActiveTurnID)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		if lastCursor.Valid {
			return rollback(errors.New("journal has no recovery checkpoint"))
		}
		if _, err := conn.ExecContext(
			ctx,
			"INSERT INTO checkpoint (id, source_id, through_cursor, harness_running, active_turn_id) VALUES (1, ?, 0, 0, '')",
			sourceID,
		); err != nil {
			return rollback(fmt.Errorf("create journal recovery checkpoint: %w", err))
		}
		checkpointSource, through = sourceID, 0
	case err != nil:
		return rollback(fmt.Errorf("read journal recovery checkpoint: %w", err))
	default:
	}
	if checkpointSource != sourceID {
		return rollback(errors.New("journal recovery checkpoint source does not match"))
	}
	expectedThrough := int64(0)
	if lastCursor.Valid {
		expectedThrough = lastCursor.Int64
	}
	if through != expectedThrough {
		return rollback(errors.New("journal recovery checkpoint does not match stored prefix"))
	}
	if lastCursor.Valid {
		var cursor int64
		var payload []byte
		if err := conn.QueryRowContext(
			ctx,
			"SELECT cursor, payload FROM event_entry ORDER BY cursor DESC LIMIT 1",
		).Scan(&cursor, &payload); err != nil {
			return rollback(fmt.Errorf("read final journal entry: %w", err))
		}
		if _, err := decodeEntry(payload, sourceID, uint64(cursor)); err != nil {
			return rollback(err)
		}
	}
	var seal *SealState
	var sealCursor sql.NullInt64
	var sealUID sql.NullString
	err = conn.QueryRowContext(ctx, "SELECT cursor, incarnation_uid FROM seal WHERE id = 1").Scan(&sealCursor, &sealUID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return rollback(fmt.Errorf("read journal seal: %w", err))
	}
	if sealCursor.Valid {
		if sealCursor.Int64 != expectedThrough {
			return rollback(errors.New("journal has Events after its seal"))
		}
		if !sealUID.Valid || sealUID.String == "" {
			return rollback(errors.New("journal seal does not name a runner incarnation UID"))
		}
		seal = &SealState{Cursor: uint64(sealCursor.Int64), IncarnationUID: sealUID.String}
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		return closeOnError(fmt.Errorf("finish journal recovery: %w", err))
	}
	state.ThroughCursor = uint64(through)
	journal := &Journal{db: db, conn: conn, sourceID: sourceID, state: state, seal: seal, changed: make(chan struct{})}
	journal.publishAtomicsLocked()
	return journal, nil
}

var journalTemporarySchema = []string{
	`CREATE TEMP TABLE IF NOT EXISTS adapter_item (
		adapter_id VARCHAR NOT NULL,
		item_id VARCHAR NOT NULL,
		PRIMARY KEY (adapter_id, item_id)
	)`,
	`CREATE TEMP TABLE IF NOT EXISTS adapter_message (
		adapter_id VARCHAR NOT NULL,
		message_id VARCHAR NOT NULL,
		next_block INTEGER NOT NULL,
		PRIMARY KEY (adapter_id, message_id)
	)`,
}

func (j *Journal) SourceID() string { return j.sourceID }

func (j *Journal) RecoveryState() RecoveryState {
	state := j.publishedState.Load()
	if state == nil {
		return RecoveryState{}
	}
	return *state
}

func (j *Journal) LastCursor() uint64 {
	return j.RecoveryState().ThroughCursor
}

func (j *Journal) SealedCursor() (uint64, bool) {
	seal := j.publishedSeal.Load()
	if seal == nil {
		return 0, false
	}
	return seal.Cursor, true
}

func (j *Journal) SealState() *SealState {
	seal := j.publishedSeal.Load()
	if seal == nil {
		return nil
	}
	copy := *seal
	return &copy
}

func (j *Journal) Close() error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.closed {
		return nil
	}
	j.closed = true
	return errors.Join(j.conn.Close(), j.db.Close())
}

// Since returns a bounded, dense page after afterCursor through the committed end.
func (j *Journal) Since(ctx context.Context, afterCursor uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	if limit < 1 {
		return nil, errors.New("replay requires a positive page size")
	}
	inBatch := j.ownsBatch(ctx)
	batch, _ := ctx.Value(batchContextKey{}).(*journalBatch)
	if !inBatch {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return nil, err
	}
	through := j.state.ThroughCursor
	if inBatch && batch.active {
		var maximum sql.NullInt64
		if err := j.conn.QueryRowContext(ctx, "SELECT MAX(cursor) FROM event_entry").Scan(&maximum); err != nil {
			return nil, err
		}
		through = 0
		if maximum.Valid {
			through = uint64(maximum.Int64)
		}
	}
	if afterCursor > through {
		return nil, errors.New("replay cursor is beyond the journal end")
	}
	rows, err := j.conn.QueryContext(
		ctx,
		"SELECT cursor, payload FROM event_entry WHERE cursor > ? AND cursor <= ? ORDER BY cursor LIMIT ?",
		afterCursor, through, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	entries := make([]*eventlogpb.EventEntry, 0, limit)
	expected := afterCursor + 1
	for rows.Next() {
		var cursor int64
		var payload []byte
		if err := rows.Scan(&cursor, &payload); err != nil {
			return nil, err
		}
		if cursor < 0 || uint64(cursor) != expected {
			return nil, errors.New("gap in stored runner EventEntry prefix")
		}
		entry, err := decodeEntry(payload, j.sourceID, expected)
		if err != nil {
			return nil, err
		}
		entries = append(entries, entry)
		expected++
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	wanted := min(uint64(limit), through-afterCursor)
	if uint64(len(entries)) != wanted {
		return nil, errors.New("incomplete stored runner EventEntry prefix")
	}
	return entries, nil
}

func decodeEntry(payload []byte, sourceID string, cursor uint64) (*eventlogpb.EventEntry, error) {
	entry := &eventlogpb.EventEntry{}
	if err := proto.Unmarshal(payload, entry); err != nil {
		return nil, fmt.Errorf("decode stored runner EventEntry at cursor %d: %w", cursor, err)
	}
	if entry.GetCursor() != cursor || entry.GetOrigin().GetSourceId() != sourceID || entry.GetOrigin().GetSequence() != cursor {
		return nil, errors.New("invalid stored runner EventEntry origin or cursor")
	}
	return entry, nil
}

func (j *Journal) WaitBeyond(ctx context.Context, cursor uint64) error {
	for {
		j.mu.Lock()
		if err := j.checkWritableLocked(); err != nil {
			j.mu.Unlock()
			return err
		}
		if j.state.ThroughCursor > cursor || j.seal != nil {
			j.mu.Unlock()
			return nil
		}
		changed := j.changed
		j.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

func (j *Journal) checkWritableLocked() error {
	if j.closed {
		return errors.New("journal is closed")
	}
	if j.failure != nil {
		return fmt.Errorf("%w: %v", ErrFailed, j.failure)
	}
	return nil
}

func (j *Journal) notifyLocked() {
	close(j.changed)
	j.changed = make(chan struct{})
}

func (j *Journal) publishLocked(entries []*eventlogpb.EventEntry, sealChanged bool, seal *SealState) {
	if len(entries) > 0 {
		state := j.state
		for _, entry := range entries {
			applyCheckpoint(&state, entry)
		}
		j.state = state
	}
	if sealChanged {
		if seal == nil {
			j.seal = nil
		} else {
			copy := *seal
			j.seal = &copy
		}
	}
	j.publishAtomicsLocked()
	if len(entries) > 0 || sealChanged {
		j.notifyLocked()
	}
}

func (j *Journal) publishAtomicsLocked() {
	state := j.state
	j.publishedState.Store(&state)
	if j.seal == nil {
		j.publishedSeal.Store(nil)
	} else {
		seal := *j.seal
		j.publishedSeal.Store(&seal)
	}
}

func applyCheckpoint(state *RecoveryState, entry *eventlogpb.EventEntry) {
	state.ThroughCursor = entry.GetCursor()
	switch event := entry.GetEvent(); {
	case event.GetHarnessStarted() != nil:
		state.HarnessRunning = true
	case event.GetHarnessLost() != nil || event.GetHarnessExited() != nil:
		state.HarnessRunning = false
	case event.GetTurnStarted() != nil:
		state.ActiveTurnID = event.GetTurnStarted().GetTurnId()
	case event.GetTurnCompleted() != nil:
		if state.ActiveTurnID != "" && state.ActiveTurnID == event.GetTurnCompleted().GetTurnId() {
			state.ActiveTurnID = ""
		}
	}
}

func (j *Journal) WithBatch(ctx context.Context, callback func(context.Context) error) error {
	j.mu.Lock()
	batch := &journalBatch{journal: j, locked: true, sealOverride: cloneSeal(j.seal)}
	defer func() {
		if batch.locked {
			j.mu.Unlock()
		}
	}()
	if err := j.checkWritableLocked(); err != nil {
		return err
	}
	batchCtx := context.WithValue(ctx, batchContextKey{}, batch)
	if err := callback(batchCtx); err != nil {
		if batch.active {
			_, rollbackErr := j.conn.ExecContext(context.Background(), "ROLLBACK")
			j.failure = err
			j.notifyLocked()
			return errors.Join(err, rollbackErr)
		}
		return err
	}
	return j.CommitBatch(batchCtx)
}

// CommitBatch publishes the calling batch and releases writer ownership before a
// native write or reply handoff. Later operations in the callback reacquire it.
func (j *Journal) CommitBatch(ctx context.Context) error {
	batch, ok := ctx.Value(batchContextKey{}).(*journalBatch)
	if !ok || batch.journal != j || !batch.locked {
		return nil
	}
	defer func() {
		batch.locked = false
		j.mu.Unlock()
	}()
	if !batch.active {
		return nil
	}
	if _, err := j.conn.ExecContext(ctx, "COMMIT"); err != nil {
		return j.failBatchLocked(batch, err)
	}
	j.publishLocked(batch.appended, batch.sealChanged, batch.sealOverride)
	batch.active = false
	batch.appended = nil
	batch.sealChanged = false
	batch.sealOverride = cloneSeal(j.seal)
	return nil
}

func (j *Journal) lockBatch(batch *journalBatch) {
	if !batch.locked {
		j.mu.Lock()
		batch.locked = true
		batch.sealOverride = cloneSeal(j.seal)
	}
}

func cloneSeal(seal *SealState) *SealState {
	if seal == nil {
		return nil
	}
	copy := *seal
	return &copy
}

func (j *Journal) ensureBatch(ctx context.Context, batch *journalBatch) error {
	j.lockBatch(batch)
	if batch.active {
		return nil
	}
	if err := j.checkWritableLocked(); err != nil {
		return err
	}
	if _, err := j.conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		j.failure = err
		j.notifyLocked()
		return &StorageError{Err: err}
	}
	batch.active = true
	return nil
}

func (j *Journal) write(ctx context.Context, operation func(*journalBatch) error) error {
	if batch, ok := ctx.Value(batchContextKey{}).(*journalBatch); ok && batch.journal == j {
		if err := j.ensureBatch(ctx, batch); err != nil {
			return err
		}
		batch.sequence++
		savepoint := fmt.Sprintf("journal_op_%d", batch.sequence)
		if _, err := j.conn.ExecContext(ctx, "SAVEPOINT "+savepoint); err != nil {
			return j.failBatchLocked(batch, err)
		}
		beforeAppended := len(batch.appended)
		beforeSeal, beforeSealChanged := cloneSeal(batch.sealOverride), batch.sealChanged
		if err := operation(batch); err != nil {
			_, rollbackErr := j.conn.ExecContext(context.Background(), "ROLLBACK TO "+savepoint)
			_, releaseErr := j.conn.ExecContext(context.Background(), "RELEASE "+savepoint)
			batch.appended = batch.appended[:beforeAppended]
			batch.sealOverride, batch.sealChanged = beforeSeal, beforeSealChanged
			if isStorageError(err) || rollbackErr != nil || releaseErr != nil {
				return j.failBatchLocked(batch, errors.Join(err, rollbackErr, releaseErr))
			}
			return err
		}
		if _, err := j.conn.ExecContext(ctx, "RELEASE "+savepoint); err != nil {
			return j.failBatchLocked(batch, err)
		}
		return nil
	}

	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.checkWritableLocked(); err != nil {
		return err
	}
	batch := &journalBatch{journal: j, sealOverride: cloneSeal(j.seal)}
	if _, err := j.conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		j.failure = err
		j.notifyLocked()
		return &StorageError{Err: err}
	}
	if err := operation(batch); err != nil {
		_, rollbackErr := j.conn.ExecContext(context.Background(), "ROLLBACK")
		if isStorageError(err) || rollbackErr != nil {
			j.failure = errors.Join(err, rollbackErr)
			j.notifyLocked()
			return &StorageError{Err: j.failure}
		}
		return err
	}
	if _, err := j.conn.ExecContext(ctx, "COMMIT"); err != nil {
		return j.failBatchLocked(batch, err)
	}
	j.publishLocked(batch.appended, batch.sealChanged, batch.sealOverride)
	return nil
}

// A failed COMMIT can leave its transaction and locks active. Release them,
// but keep the writer failed: only reopening may reconcile durable evidence.
func (j *Journal) failBatchLocked(batch *journalBatch, err error) error {
	_, rollbackErr := j.conn.ExecContext(context.Background(), "ROLLBACK")
	batch.active = false
	j.failure = errors.Join(err, rollbackErr)
	j.notifyLocked()
	return &StorageError{Err: j.failure}
}

func isStorageError(err error) bool {
	var storageErr *StorageError
	return errors.As(err, &storageErr)
}

func storageError(op string, err error) error {
	return &StorageError{Err: fmt.Errorf("%s: %w", op, err)}
}

func (j *Journal) appendEntry(ctx context.Context, batch *journalBatch, event *eventpb.Event, options AppendOptions, allowSealed bool) (*eventlogpb.EventEntry, error) {
	if batch.sealOverride != nil && !allowSealed {
		return nil, ErrSealed
	}
	if event == nil || event.ProtoReflect().WhichOneof(event.ProtoReflect().Descriptor().Oneofs().ByName("observation")) == nil {
		return nil, errors.New("journal Event requires an observation")
	}
	var maximum sql.NullInt64
	if err := j.conn.QueryRowContext(ctx, "SELECT MAX(cursor) FROM event_entry").Scan(&maximum); err != nil {
		return nil, storageError("read journal cursor", err)
	}
	last := int64(0)
	if maximum.Valid {
		last = maximum.Int64
	}
	if last < 0 || uint64(last) != j.state.ThroughCursor+uint64(len(batch.appended)) {
		return nil, ErrChangedOutside
	}
	if last == math.MaxInt64 {
		return nil, errors.New("journal cursor exhausted")
	}
	var checkpointSource, activeTurn string
	var through int64
	var harnessRunning bool
	if err := j.conn.QueryRowContext(
		ctx,
		"SELECT source_id, through_cursor, harness_running, active_turn_id FROM checkpoint WHERE id = 1",
	).Scan(&checkpointSource, &through, &harnessRunning, &activeTurn); err != nil {
		return nil, storageError("read journal checkpoint", err)
	}
	if checkpointSource != j.sourceID || through != last {
		return nil, storageError("validate journal checkpoint", ErrChangedOutside)
	}
	if completed := event.GetTurnCompleted(); completed != nil && (activeTurn == "" || activeTurn != completed.GetTurnId()) {
		// Native protocols can repeat or deliver stale completion notifications.
		// Keep their raw Native frame, but do not let them advance the normalized
		// log or clear a different active turn.
		return nil, nil
	}
	cursor := uint64(last + 1)
	copyEvent := proto.Clone(event).(*eventpb.Event)
	copyEvent.SourceSequences = append(copyEvent.SourceSequences[:0], options.Sources...)
	copyEvent.At = timestamppb.New(time.Now().UTC())
	entry := &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: j.sourceID, Sequence: cursor},
		Event:  copyEvent,
	}
	payload, err := proto.Marshal(entry)
	if err != nil {
		return nil, storageError("encode runner EventEntry", err)
	}
	if _, err := j.conn.ExecContext(ctx, "INSERT INTO event_entry (cursor, payload) VALUES (?, ?)", cursor, payload); err != nil {
		return nil, storageError("insert runner EventEntry", err)
	}
	nextState := RecoveryState{ThroughCursor: uint64(through), HarnessRunning: harnessRunning, ActiveTurnID: activeTurn}
	applyCheckpoint(&nextState, entry)
	if _, err := j.conn.ExecContext(
		ctx,
		"UPDATE checkpoint SET through_cursor = ?, harness_running = ?, active_turn_id = ? WHERE id = 1",
		nextState.ThroughCursor, nextState.HarnessRunning, nextState.ActiveTurnID,
	); err != nil {
		return nil, storageError("update journal checkpoint", err)
	}
	if started := copyEvent.GetTurnStarted(); started != nil {
		if _, err := j.conn.ExecContext(
			ctx,
			"INSERT INTO turn_boundary (turn_id, start_cursor, end_cursor, status) VALUES (?, ?, NULL, NULL)",
			started.GetTurnId(), cursor,
		); err != nil {
			return nil, storageError("insert turn boundary", err)
		}
	}
	if completed := copyEvent.GetTurnCompleted(); completed != nil {
		result, err := j.conn.ExecContext(
			ctx,
			"UPDATE turn_boundary SET end_cursor = ?, status = ? WHERE turn_id = ? AND end_cursor IS NULL",
			cursor, completed.GetStatus(), completed.GetTurnId(),
		)
		if err != nil {
			return nil, storageError("complete turn boundary", err)
		}
		if rows, err := result.RowsAffected(); err != nil {
			return nil, storageError("verify completed turn boundary", err)
		} else if rows != 1 {
			return nil, storageError("verify completed turn boundary", fmt.Errorf("turn %q has no open boundary", completed.GetTurnId()))
		}
	}
	if checkpoint := copyEvent.GetDebugCheckpoint(); checkpoint != nil {
		if _, err := j.conn.ExecContext(
			ctx,
			"INSERT OR IGNORE INTO debug_checkpoint (name, command_id) VALUES (?, ?)",
			checkpoint.GetName(), checkpoint.GetCommandId(),
		); err != nil {
			return nil, storageError("record debug checkpoint", err)
		}
	}
	for _, commandID := range options.TerminalCommandIDs {
		var terminal sql.NullInt64
		err := j.conn.QueryRowContext(ctx, "SELECT terminal_cursor FROM command WHERE command_id = ?", commandID).Scan(&terminal)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, fmt.Errorf("%w %q", ErrUnknownCommand, commandID)
		}
		if err != nil {
			return nil, storageError("read command receipt", err)
		}
		if !terminal.Valid {
			if _, err := j.conn.ExecContext(
				ctx,
				"UPDATE command SET terminal_cursor = ? WHERE command_id = ? AND terminal_cursor IS NULL",
				cursor, commandID,
			); err != nil {
				return nil, storageError("write command receipt", err)
			}
		}
	}
	batch.appended = append(batch.appended, entry)
	return proto.Clone(entry).(*eventlogpb.EventEntry), nil
}

func (j *Journal) Append(ctx context.Context, event *eventpb.Event, options AppendOptions) (*eventlogpb.EventEntry, error) {
	// A stale or duplicate TurnCompleted is intentionally ignored and returns
	// (nil, nil). Callers must treat a nil entry as an idempotent no-op.
	var result *eventlogpb.EventEntry
	err := j.write(ctx, func(batch *journalBatch) error {
		entry, err := j.appendEntry(ctx, batch, event, options, false)
		if err == nil {
			result = entry
		}
		return err
	})
	return result, err
}

func (j *Journal) Admit(ctx context.Context, command *commandpb.Command, expectedActiveTurnID string) (*eventlogpb.EventEntry, bool, error) {
	if command == nil || command.GetCommandId() == "" || command.ProtoReflect().WhichOneof(command.ProtoReflect().Descriptor().Oneofs().ByName("operation")) == nil {
		return nil, false, errors.New("command requires command_id and operation")
	}
	commandCopy := proto.Clone(command).(*commandpb.Command)
	payload, err := proto.Marshal(commandCopy)
	if err != nil {
		return nil, false, err
	}
	var result *eventlogpb.EventEntry
	newlyAdmitted := false
	err = j.write(ctx, func(batch *journalBatch) error {
		var previous []byte
		var admittedCursor int64
		lookupErr := j.conn.QueryRowContext(
			ctx,
			"SELECT payload, admitted_cursor FROM command WHERE command_id = ?", commandCopy.GetCommandId(),
		).Scan(&previous, &admittedCursor)
		if lookupErr == nil {
			oldCommand := &commandpb.Command{}
			if err := proto.Unmarshal(previous, oldCommand); err != nil {
				return storageError("decode admitted command", err)
			}
			if !proto.Equal(oldCommand, commandCopy) {
				return fmt.Errorf("%w: command ID %q", ErrCommandConflict, commandCopy.GetCommandId())
			}
			var payload []byte
			if err := j.conn.QueryRowContext(
				ctx,
				"SELECT payload FROM event_entry WHERE cursor = ?", admittedCursor,
			).Scan(&payload); err != nil {
				return storageError("read original command admission", err)
			}
			result = &eventlogpb.EventEntry{}
			if err := proto.Unmarshal(payload, result); err != nil {
				return storageError("decode original command admission", err)
			}
			return nil
		}
		if !errors.Is(lookupErr, sql.ErrNoRows) {
			return storageError("look up command", lookupErr)
		}
		if batch.sealOverride != nil {
			return ErrSealed
		}
		if expectedActiveTurnID != "" {
			var activeTurn string
			if err := j.conn.QueryRowContext(ctx, "SELECT active_turn_id FROM checkpoint WHERE id = 1").Scan(&activeTurn); err != nil {
				return storageError("read active turn", err)
			}
			if activeTurn != expectedActiveTurnID {
				return fmt.Errorf("%w: turn %q", ErrActiveTurnChanged, expectedActiveTurnID)
			}
		}
		event := &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{Command: commandCopy}}}
		entry, err := j.appendEntry(ctx, batch, event, AppendOptions{}, false)
		if err != nil {
			return err
		}
		if _, err := j.conn.ExecContext(
			ctx,
			"INSERT INTO command (command_id, payload, admitted_cursor, dispatch_planned, terminal_cursor) VALUES (?, ?, ?, 0, NULL)",
			commandCopy.GetCommandId(), payload, entry.GetCursor(),
		); err != nil {
			return storageError("insert command admission", err)
		}
		result, newlyAdmitted = entry, true
		return nil
	})
	return result, newlyAdmitted, err
}

func (j *Journal) DispatchPlanned(ctx context.Context, commandID string) (bool, error) {
	planned := false
	err := j.write(ctx, func(_ *journalBatch) error {
		var terminal sql.NullInt64
		err := j.conn.QueryRowContext(
			ctx,
			"SELECT terminal_cursor FROM command WHERE command_id = ?", commandID,
		).Scan(&terminal)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w %q", ErrUnknownCommand, commandID)
		}
		if err != nil {
			return storageError("read dispatch command", err)
		}
		if terminal.Valid {
			return nil
		}
		if _, err := j.conn.ExecContext(
			ctx,
			"UPDATE command SET dispatch_planned = 1 WHERE command_id = ? AND terminal_cursor IS NULL",
			commandID,
		); err != nil {
			return storageError("mark command dispatch planned", err)
		}
		planned = true
		return nil
	})
	return planned, err
}

func (j *Journal) GetCommand(ctx context.Context, commandID string) (*harness.CommandRecord, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return nil, err
	}
	return j.getCommandLocked(ctx, commandID)
}

func (j *Journal) getCommandLocked(ctx context.Context, commandID string) (*harness.CommandRecord, error) {
	var payload []byte
	var admitted int64
	var dispatch bool
	var terminal sql.NullInt64
	err := j.conn.QueryRowContext(
		ctx,
		"SELECT payload, admitted_cursor, dispatch_planned, terminal_cursor FROM command WHERE command_id = ?",
		commandID,
	).Scan(&payload, &admitted, &dispatch, &terminal)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return decodeCommandRecord(payload, admitted, dispatch, terminal)
}

func decodeCommandRecord(payload []byte, admitted int64, dispatch bool, terminal sql.NullInt64) (*harness.CommandRecord, error) {
	command := &commandpb.Command{}
	if err := proto.Unmarshal(payload, command); err != nil {
		return nil, err
	}
	record := &harness.CommandRecord{
		Command: command, AdmittedCursor: uint64(admitted), DispatchPlanned: dispatch,
	}
	if terminal.Valid {
		value := uint64(terminal.Int64)
		record.TerminalCursor = &value
	}
	return record, nil
}

func (j *Journal) PendingCommands(ctx context.Context) ([]*commandpb.Command, error) {
	records, err := j.PendingCommandRecords(ctx)
	if err != nil {
		return nil, err
	}
	commands := make([]*commandpb.Command, 0, len(records))
	for _, record := range records {
		commands = append(commands, record.Command)
	}
	return commands, nil
}

func (j *Journal) HasStartedTurn(ctx context.Context) (bool, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return false, err
	}
	var exists bool
	if err := j.conn.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM turn_boundary)").Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
}

func (j *Journal) ReachedCheckpoint(ctx context.Context, name, commandID string) (bool, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return false, err
	}
	var exists bool
	if err := j.conn.QueryRowContext(
		ctx,
		"SELECT EXISTS(SELECT 1 FROM debug_checkpoint WHERE name = ? AND command_id = ?)", name, commandID,
	).Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
}

func (j *Journal) RecoveryTurns(ctx context.Context) ([]string, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.checkWritableLocked(); err != nil {
		return nil, err
	}
	rows, err := j.conn.QueryContext(ctx, `
		SELECT turn_id FROM turn_boundary
		WHERE start_cursor >= (
			SELECT COALESCE(MAX(start_cursor), 0) FROM turn_boundary WHERE status = ?
		)
		ORDER BY start_cursor`, eventpb.TurnStatus_TURN_STATUS_COMPLETED)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	turns := []string{}
	for rows.Next() {
		var turnID string
		if err := rows.Scan(&turnID); err != nil {
			return nil, err
		}
		turns = append(turns, turnID)
	}
	return turns, rows.Err()
}

func (j *Journal) UnreconciledProcessLostTurns(ctx context.Context) ([]string, error) {
	j.mu.Lock()
	if err := j.checkWritableLocked(); err != nil {
		j.mu.Unlock()
		return nil, err
	}
	rows, err := j.conn.QueryContext(
		ctx,
		"SELECT turn_id, end_cursor FROM turn_boundary WHERE status = ? AND end_cursor IS NOT NULL",
		eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST,
	)
	if err != nil {
		j.mu.Unlock()
		return nil, err
	}
	lost := map[string]uint64{}
	for rows.Next() {
		var turnID string
		var end int64
		if err := rows.Scan(&turnID, &end); err != nil {
			rows.Close()
			j.mu.Unlock()
			return nil, err
		}
		lost[turnID] = uint64(end)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		j.mu.Unlock()
		return nil, err
	}
	rows.Close()
	through := j.state.ThroughCursor
	j.mu.Unlock()
	if len(lost) == 0 {
		return []string{}, nil
	}
	cursor := uint64(math.MaxUint64)
	for _, end := range lost {
		if end < cursor {
			cursor = end
		}
	}
	attachedAfterLoss := map[string]bool{}
	for cursor < through && len(lost) > 0 {
		entries, err := j.Since(ctx, cursor, journalPageSize)
		if err != nil {
			return nil, err
		}
		if len(entries) == 0 {
			break
		}
		for _, entry := range entries {
			cursor = entry.GetCursor()
			event := entry.GetEvent()
			if event.GetHarnessStarted() != nil {
				for turnID, endedAt := range lost {
					if cursor > endedAt {
						attachedAfterLoss[turnID] = true
					}
				}
			} else if reconciliation := event.GetConversationReconciled(); reconciliation != nil && attachedAfterLoss[reconciliation.GetTurnId()] {
				delete(lost, reconciliation.GetTurnId())
				delete(attachedAfterLoss, reconciliation.GetTurnId())
			}
		}
	}
	turns := make([]string, 0, len(lost))
	for turnID := range lost {
		turns = append(turns, turnID)
	}
	sort.Slice(turns, func(i, k int) bool { return lost[turns[i]] < lost[turns[k]] })
	return turns, nil
}

func (j *Journal) turnBoundary(ctx context.Context, turnID string) (start, end uint64, err error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return 0, 0, err
	}
	var startCursor int64
	var endCursor sql.NullInt64
	err = j.conn.QueryRowContext(
		ctx,
		"SELECT start_cursor, end_cursor FROM turn_boundary WHERE turn_id = ?", turnID,
	).Scan(&startCursor, &endCursor)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, 0, fmt.Errorf("%w %q", harness.ErrUnknownRecoveryTurn, turnID)
	}
	if err != nil {
		return 0, 0, err
	}
	through := j.state.ThroughCursor
	if batch, ok := ctx.Value(batchContextKey{}).(*journalBatch); ok && batch.journal == j && batch.active {
		var maximum sql.NullInt64
		if err := j.conn.QueryRowContext(ctx, "SELECT MAX(cursor) FROM event_entry").Scan(&maximum); err != nil {
			return 0, 0, err
		}
		through = 0
		if maximum.Valid {
			through = uint64(maximum.Int64)
		}
	}
	end = through
	if endCursor.Valid {
		end = uint64(endCursor.Int64)
	}
	return uint64(startCursor), end, nil
}

func (j *Journal) turnEvents(ctx context.Context, turnID string, includePrefix bool) ([]*eventlogpb.EventEntry, error) {
	start, end, err := j.turnBoundary(ctx, turnID)
	if err != nil {
		return nil, err
	}
	after := start - 1
	if includePrefix {
		after = 0
	}
	entries := make([]*eventlogpb.EventEntry, 0)
	for after < end {
		page, err := j.Since(ctx, after, journalPageSize)
		if err != nil {
			return nil, err
		}
		if len(page) == 0 {
			return nil, fmt.Errorf("missing journal events for recovery turn %q", turnID)
		}
		for _, entry := range page {
			if entry.GetCursor() > end {
				break
			}
			entries = append(entries, entry)
			after = entry.GetCursor()
		}
	}
	return entries, nil
}

func (j *Journal) TurnEvents(ctx context.Context, turnID string) ([]*eventlogpb.EventEntry, error) {
	return j.turnEvents(ctx, turnID, false)
}

func (j *Journal) ownsBatch(ctx context.Context) bool {
	batch, ok := ctx.Value(batchContextKey{}).(*journalBatch)
	if !ok || batch.journal != j {
		return false
	}
	j.lockBatch(batch)
	return true
}

func (j *Journal) HasAdapterItem(ctx context.Context, adapterID, itemID string) (bool, error) {
	if !j.ownsBatch(ctx) {
		j.mu.Lock()
		defer j.mu.Unlock()
	}
	if err := j.checkWritableLocked(); err != nil {
		return false, err
	}
	var exists bool
	if err := j.conn.QueryRowContext(
		ctx,
		"SELECT EXISTS(SELECT 1 FROM adapter_item WHERE adapter_id = ? AND item_id = ?)", adapterID, itemID,
	).Scan(&exists); err != nil {
		return false, err
	}
	return exists, nil
}

func (j *Journal) RememberAdapterItem(ctx context.Context, adapterID, itemID string) (bool, error) {
	first := false
	err := j.write(ctx, func(_ *journalBatch) error {
		result, err := j.conn.ExecContext(
			ctx,
			"INSERT OR IGNORE INTO adapter_item (adapter_id, item_id) VALUES (?, ?)", adapterID, itemID,
		)
		if err != nil {
			return storageError("remember adapter item", err)
		}
		count, err := result.RowsAffected()
		if err != nil {
			return storageError("read adapter item result", err)
		}
		first = count == 1
		return nil
	})
	return first, err
}

func (j *Journal) NextAdapterBlock(ctx context.Context, adapterID, messageID string) (int, error) {
	var block int
	err := j.write(ctx, func(_ *journalBatch) error {
		var next int
		err := j.conn.QueryRowContext(
			ctx,
			"SELECT next_block FROM adapter_message WHERE adapter_id = ? AND message_id = ?", adapterID, messageID,
		).Scan(&next)
		if errors.Is(err, sql.ErrNoRows) {
			if _, err := j.conn.ExecContext(
				ctx,
				"INSERT INTO adapter_message (adapter_id, message_id, next_block) VALUES (?, ?, 1)", adapterID, messageID,
			); err != nil {
				return storageError("create adapter message block counter", err)
			}
			block = 0
			return nil
		}
		if err != nil {
			return storageError("read adapter message block counter", err)
		}
		if _, err := j.conn.ExecContext(
			ctx,
			"UPDATE adapter_message SET next_block = ? WHERE adapter_id = ? AND message_id = ?", next+1, adapterID, messageID,
		); err != nil {
			return storageError("advance adapter message block counter", err)
		}
		block = next
		return nil
	})
	return block, err
}

func (j *Journal) Seal(ctx context.Context, incarnationUID string) (uint64, error) {
	if incarnationUID == "" {
		return 0, errors.New("a journal seal requires a runner incarnation UID")
	}
	var cursor uint64
	err := j.write(ctx, func(batch *journalBatch) error {
		var existing int64
		var existingUID string
		err := j.conn.QueryRowContext(ctx, "SELECT cursor, incarnation_uid FROM seal WHERE id = 1").Scan(&existing, &existingUID)
		if err == nil {
			cursor = uint64(existing)
			batch.sealOverride = &SealState{Cursor: cursor, IncarnationUID: existingUID}
			batch.sealChanged = true
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return storageError("read journal seal", err)
		}
		var maximum sql.NullInt64
		if err := j.conn.QueryRowContext(ctx, "SELECT MAX(cursor) FROM event_entry").Scan(&maximum); err != nil {
			return storageError("read cursor to seal", err)
		}
		if maximum.Valid {
			cursor = uint64(maximum.Int64)
		}
		if _, err := j.conn.ExecContext(
			ctx,
			"INSERT INTO seal (id, cursor, incarnation_uid) VALUES (1, ?, ?)", cursor, incarnationUID,
		); err != nil {
			return storageError("seal journal", err)
		}
		batch.sealOverride = &SealState{Cursor: cursor, IncarnationUID: incarnationUID}
		batch.sealChanged = true
		return nil
	})
	return cursor, err
}

func (j *Journal) Reopen(ctx context.Context, incarnationUID string) error {
	if incarnationUID == "" {
		return errors.New("journal reopening requires a runner incarnation UID")
	}
	return j.write(ctx, func(batch *journalBatch) error {
		if _, err := j.conn.ExecContext(ctx, "DELETE FROM seal"); err != nil {
			return storageError("remove journal seal", err)
		}
		batch.sealOverride = nil
		batch.sealChanged = true
		_, err := j.appendEntry(
			ctx, batch,
			&eventpb.Event{Observation: &eventpb.Event_JournalReopened{JournalReopened: &eventpb.JournalReopened{IncarnationUid: incarnationUID}}},
			AppendOptions{}, true,
		)
		return err
	})
}
