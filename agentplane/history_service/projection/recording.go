package projection

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/settledcursors"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// ErrInconsistentFold means the stored fold rows disagree with each other or with the history, so the
// fold cannot advance until they are repaired or rebuilt.
var ErrInconsistentFold = errors.New("inconsistent thread fold")

// ReplicationError means the runner stream conflicts with the archived prefix or skips an entry, or the
// fold's epoch must be reset.
type ReplicationError struct {
	// Cursor is the entry the conflict was found at, when it names one.
	Cursor  *uint64
	Message string
}

func (e *ReplicationError) Error() string { return e.Message }

func replication(cursor *uint64, format string, args ...any) *ReplicationError {
	return &ReplicationError{Cursor: cursor, Message: fmt.Sprintf(format, args...)}
}

type checkpoint struct {
	sourceID        *string
	projectionEpoch string
	throughCursor   uint64
}

func lockCheckpoint(ctx context.Context, tx *sql.Tx, threadID uuid.UUID) (*checkpoint, error) {
	var found checkpoint
	err := tx.QueryRowContext(ctx, `SELECT source_id, projection_epoch, through_cursor FROM thread_checkpoint
		WHERE thread_id = $1 FOR UPDATE`, threadID).Scan(&found.sourceID, &found.projectionEpoch, &found.throughCursor)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &found, err
}

func insertEntity(ctx context.Context, tx *sql.Tx, row threadfold.EntityRow, index uint64) (uint64, error) {
	args := []any{
		row.ThreadID, row.ProjectionEpoch, row.EntityKind, row.EntityID, row.Cursor, row.RevisionCursor,
		row.Pending, row.TurnID, string(row.State),
	}
	for _, reference := range []*threadfold.Reference{row.TextRef, row.ArgumentsRef, row.OutputRef, row.InputRef} {
		if reference == nil {
			args = append(args, nil)
			continue
		}
		encoded, err := json.Marshal(reference)
		if err != nil {
			return 0, err
		}
		args = append(args, string(encoded))
	}
	// A revision takes the conflict path, which leaves the index it was numbered with.
	var stored uint64
	err := tx.QueryRowContext(ctx, `INSERT INTO thread_entity (thread_id, projection_epoch, entity_kind, entity_id,
			cursor, revision_cursor, pending, turn_id, state, text_ref, arguments_ref, output_ref, input_ref, entity_index)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12::jsonb, $13::jsonb, $14)
		ON CONFLICT (thread_id, projection_epoch, entity_kind, entity_id) DO UPDATE SET
			cursor = EXCLUDED.cursor, revision_cursor = EXCLUDED.revision_cursor, pending = EXCLUDED.pending,
			turn_id = EXCLUDED.turn_id, state = EXCLUDED.state, text_ref = EXCLUDED.text_ref,
			arguments_ref = EXCLUDED.arguments_ref, output_ref = EXCLUDED.output_ref, input_ref = EXCLUDED.input_ref
		RETURNING entity_index`, append(args, index)...).Scan(&stored)
	return stored, err
}

// RecordEmptyThreadFold materializes an authenticated runner's empty prefix, without inventing an Event or its
// source. It reports whether it did: a Thread that already has a fold is left alone.
func RecordEmptyThreadFold(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, projectionEpoch string) (bool, error) {
	var exists bool
	if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM thread_checkpoint WHERE thread_id = $1)", threadID).Scan(&exists); err != nil {
		return false, err
	}
	if exists {
		return false, nil
	}
	state, err := json.Marshal(threadfold.StoredViewState{
		Operational: threadfold.Operational{Status: threadfold.StatusActive},
	})
	if err != nil {
		return false, err
	}
	if _, err := insertEntity(ctx, tx, threadfold.EntityRow{
		ThreadID: threadID, ProjectionEpoch: projectionEpoch, EntityKind: threadfold.KindViewState, EntityID: "current",
		State: state,
	}, 0); err != nil {
		return false, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO thread_checkpoint (thread_id, source_id, projection_epoch, through_cursor)
		VALUES ($1, NULL, $2, 0)`, threadID, projectionEpoch)
	return true, err
}

// resetThreadFoldEpoch clears only this Session's derived fold rows and starts its new projection at cursor 0.
// Raw History events, feed state, and sidebar metadata live outside these tables and remain intact.
func resetThreadFoldEpoch(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, projectionEpoch string) (bool, error) {
	if projectionEpoch == "" {
		return false, errors.New("projection epoch is required")
	}
	stored, err := lockCheckpoint(ctx, tx, threadID)
	if err != nil || stored == nil || stored.projectionEpoch == projectionEpoch {
		return false, err
	}
	if stored.projectionEpoch != previousFoldEpoch || projectionEpoch != FoldEpoch {
		return false, replication(nil, "thread fold epoch %q cannot reset to %q", stored.projectionEpoch, projectionEpoch)
	}
	previous, err := readViewState(ctx, tx, threadID, stored.projectionEpoch)
	if err != nil {
		return false, err
	}
	for _, table := range []string{
		"thread_payload_chunk", "thread_payload_manifest", "thread_native_link", "thread_evidence", "thread_entity",
	} {
		if _, err := tx.ExecContext(ctx, "DELETE FROM "+table+" WHERE thread_id = $1", threadID); err != nil {
			return false, err
		}
	}
	state, err := json.Marshal(threadfold.StoredViewState{
		Controls:    threadfold.StoredControls{BlockedReason: previous.Controls.BlockedReason},
		Operational: threadfold.Operational{Status: threadfold.StatusActive},
	})
	if err != nil {
		return false, err
	}
	if _, err := insertEntity(ctx, tx, threadfold.EntityRow{
		ThreadID: threadID, ProjectionEpoch: projectionEpoch, EntityKind: threadfold.KindViewState, EntityID: "current",
		State: state,
	}, 0); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE thread_checkpoint SET source_id = NULL, projection_epoch = $2, through_cursor = 0
		WHERE thread_id = $1`, threadID, projectionEpoch); err != nil {
		return false, err
	}
	return true, notify(ctx, tx, ChannelSummaries)
}

// RecordThreadFold folds an archived page into the Thread's rows, in the transaction the caller fences with
// its lease. The page must carry entries, all from sourceID.
func RecordThreadFold(
	ctx context.Context, tx *sql.Tx, threadID uuid.UUID, sourceID string,
	page *historypb.ReadSessionEventsResponse, projectionEpoch string,
) error {
	if len(page.GetEntries()) == 0 {
		return errors.New("a fold records a page with entries")
	}
	firstCursor := page.GetEntries()[0].GetCursor()
	stored, err := lockCheckpoint(ctx, tx, threadID)
	if err != nil {
		return err
	}
	var state threadfold.ViewState
	var operational *threadfold.Operational
	if stored == nil {
		if state, err = threadfold.Initial(sourceID, projectionEpoch); err != nil {
			return err
		}
	} else {
		switch {
		case stored.sourceID == nil && stored.throughCursor != 0:
			return replication(nil, "nonempty fold has no Event source")
		case stored.sourceID != nil && *stored.sourceID != sourceID:
			return replication(&firstCursor, "thread fold source changed at cursor %d", firstCursor)
		case stored.projectionEpoch != projectionEpoch:
			return replication(&firstCursor, "thread fold epoch %q must be reset for %q", stored.projectionEpoch, projectionEpoch)
		}
		if state, operational, err = foldState(ctx, tx, threadID, sourceID, stored); err != nil {
			return err
		}
	}
	batch := threadfold.EventBatch{
		SourceID: sourceID, AfterCursor: state.Position.ThroughCursor, Entries: page.GetEntries(),
		Settled: settledcursors.Unstored(page),
	}
	prior, err := priorEntities(ctx, tx, threadID, stored, batch)
	if err != nil {
		return err
	}
	result, err := threadfold.Advance(state, batch, prior)
	if err != nil {
		return err
	}
	if err := writePayloads(ctx, tx, threadID, result.PayloadWrites); err != nil {
		return err
	}
	extents, err := payloadExtents(ctx, tx, threadID, threadfold.PayloadReferences(result))
	if err != nil {
		return err
	}
	rows, err := threadfold.OrderedEntityRows(threadID, result, extents, operational)
	if err != nil {
		return err
	}
	// Numbered here rather than in the fold, which reports upserts without saying which are new.
	next, err := nextEntityIndex(ctx, tx, threadID, result.State.Position.ProjectionEpoch)
	if err != nil {
		return err
	}
	for _, row := range rows {
		index, err := insertEntity(ctx, tx, row, next)
		if err != nil {
			return err
		}
		next = max(next, index+1)
	}
	for _, evidence := range result.EvidenceUpserts {
		if _, err := tx.ExecContext(ctx, `INSERT INTO thread_evidence (thread_id, projection_epoch, entity_cursor, observation_cursor)
			VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING`,
			threadID, evidence.ProjectionEpoch, evidence.EntityCursor, evidence.ObservationCursor); err != nil {
			return err
		}
		for _, sequence := range evidence.SourceSequences {
			if _, err := tx.ExecContext(ctx, `INSERT INTO thread_native_link
				(thread_id, projection_epoch, entity_cursor, observation_cursor, source_sequence)
				VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING`,
				threadID, evidence.ProjectionEpoch, evidence.EntityCursor, evidence.ObservationCursor, sequence); err != nil {
				return err
			}
		}
	}
	if err := forgetSettled(ctx, tx, threadID, result.State.Position.ProjectionEpoch, page.GetSettlements()); err != nil {
		return err
	}
	position := result.State.Position
	_, err = tx.ExecContext(ctx, `INSERT INTO thread_checkpoint (thread_id, source_id, projection_epoch, through_cursor)
		VALUES ($1, $2, $3, $4)
		ON CONFLICT (thread_id) DO UPDATE SET source_id = EXCLUDED.source_id,
			projection_epoch = EXCLUDED.projection_epoch, through_cursor = EXCLUDED.through_cursor`,
		threadID, position.SourceID, position.ProjectionEpoch, position.ThroughCursor)
	return err
}

// SkipRemovedSuffix advances the fold over trailing cursors a cleanup deleted from stored history. They hold
// no entry to fold, but the fold is behind until its checkpoint and current view reach the end of the
// committed prefix.
func SkipRemovedSuffix(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, throughCursor uint64) error {
	stored, err := lockCheckpoint(ctx, tx, threadID)
	if err != nil {
		return err
	}
	if stored == nil || stored.sourceID == nil {
		return replication(nil, "an Event source is required to advance the fold")
	}
	if _, err := tx.ExecContext(ctx, "UPDATE thread_checkpoint SET through_cursor = $2 WHERE thread_id = $1", threadID, throughCursor); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE thread_entity SET cursor = $3, revision_cursor = $3
		WHERE thread_id = $1 AND projection_epoch = $2 AND entity_kind = $4 AND entity_id = 'current'`,
		threadID, stored.projectionEpoch, throughCursor, threadfold.KindViewState)
	return err
}

// forgetSettled drops the evidence of deltas folded before their item settled; their cursors are gone. A
// settlement commits with its completion entry, so the page that first carries that entry carries the
// settlement too, whatever an earlier page folded.
func forgetSettled(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, projectionEpoch string, settlements []*historypb.SettledDeltas) error {
	for _, settlement := range settlements {
		var entityCursor uint64
		err := tx.QueryRowContext(ctx, `SELECT cursor FROM thread_entity
			WHERE thread_id = $1 AND projection_epoch = $2 AND entity_kind = $3 AND entity_id = $4`,
			threadID, projectionEpoch, threadfold.KindItem, settlement.GetItemId()).Scan(&entityCursor)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return err
		}
		var firsts, lasts []int64
		for _, cursorRange := range settlement.GetRanges() {
			firsts = append(firsts, int64(cursorRange.GetFirst()))
			lasts = append(lasts, int64(cursorRange.GetLast()))
		}
		for _, table := range []string{"thread_native_link", "thread_evidence"} {
			if _, err := tx.ExecContext(ctx, `DELETE FROM `+table+` WHERE thread_id = $1 AND projection_epoch = $2
				AND entity_cursor = $3 AND EXISTS (SELECT 1 FROM unnest($4::bigint[], $5::bigint[]) AS settled(lo, hi)
					WHERE observation_cursor BETWEEN settled.lo AND settled.hi)`,
				threadID, projectionEpoch, entityCursor, pq.Array(firsts), pq.Array(lasts)); err != nil {
				return err
			}
		}
	}
	return nil
}

// foldState reads the fold state the stored checkpoint, whose source is sourceID, and current view row hold.
func foldState(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, sourceID string, stored *checkpoint) (threadfold.ViewState, *threadfold.Operational, error) {
	view, err := readViewState(ctx, tx, threadID, stored.projectionEpoch)
	if err != nil {
		return threadfold.ViewState{}, nil, err
	}
	return threadfold.ViewState{
		Position: threadfold.Position{SourceID: sourceID, ProjectionEpoch: stored.projectionEpoch, ThroughCursor: stored.throughCursor},
		Controls: threadfold.Controls{
			AppliedModel: view.Controls.AppliedModel, AppliedReasoningEffort: view.Controls.AppliedReasoningEffort,
			ActiveTurnID: view.Controls.ActiveTurnID, HarnessState: view.Controls.HarnessState,
			BlockedReason: view.Controls.BlockedReason,
		},
	}, &view.Operational, nil
}

func readViewState(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, projectionEpoch string) (threadfold.StoredViewState, error) {
	var state []byte
	err := tx.QueryRowContext(ctx, `SELECT state FROM thread_entity
		WHERE thread_id = $1 AND projection_epoch = $2 AND entity_kind = $3 AND entity_id = 'current'`,
		threadID, projectionEpoch, threadfold.KindViewState).Scan(&state)
	if errors.Is(err, sql.ErrNoRows) {
		return threadfold.StoredViewState{}, fmt.Errorf("%w: thread checkpoint has no current controls", ErrInconsistentFold)
	}
	if err != nil {
		return threadfold.StoredViewState{}, err
	}
	return threadfold.ReadViewState(state)
}

// SetOperational records the feed's operational state at the fold's checkpoint, and reports whether the
// stored state changed. A Thread without a fold is left alone.
func SetOperational(
	ctx context.Context, tx *sql.Tx, threadID uuid.UUID, status threadfold.OperationalStatus, message *string, errorCursor *uint64,
) (bool, error) {
	var projectionEpoch string
	var throughCursor uint64
	err := tx.QueryRowContext(ctx, "SELECT projection_epoch, through_cursor FROM thread_checkpoint WHERE thread_id = $1", threadID).
		Scan(&projectionEpoch, &throughCursor)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	view, err := readViewState(ctx, tx, threadID, projectionEpoch)
	if err != nil {
		return false, err
	}
	view.Operational = threadfold.Operational{Status: status, LastVerifiedCursor: throughCursor}
	if message != nil {
		view.Operational.FeedError = &threadfold.FeedError{Cursor: errorCursor, Message: *message}
	}
	state, err := json.Marshal(view)
	if err != nil {
		return false, err
	}
	result, err := tx.ExecContext(ctx, `UPDATE thread_entity SET state = $4::jsonb
		WHERE thread_id = $1 AND projection_epoch = $2 AND entity_kind = $3 AND entity_id = 'current'
			AND state IS DISTINCT FROM $4::jsonb`, threadID, projectionEpoch, threadfold.KindViewState, string(state))
	if err != nil {
		return false, err
	}
	changed, err := result.RowsAffected()
	return changed == 1, err
}

// priorEntities loads the rows folding batch reads back as prior state: only those it touches.
func priorEntities(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, stored *checkpoint, batch threadfold.EventBatch) (threadfold.PriorEntities, error) {
	required := threadfold.Touched(batch)
	prior := threadfold.PriorEntities{
		Items:    map[string]*threadfold.Item{},
		Commands: map[string]*threadfold.CommandSummary{},
		Bodies:   map[threadfold.PayloadRef]string{},
	}
	for _, itemID := range required.ItemIDs {
		prior.Items[itemID] = nil
	}
	for _, commandID := range required.CommandIDs {
		prior.Commands[commandID] = nil
	}
	if stored == nil {
		return prior, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT entity_kind, entity_id, cursor, revision_cursor, pending, turn_id, state,
			text_ref, arguments_ref, output_ref, input_ref
		FROM thread_entity WHERE thread_id = $1 AND projection_epoch = $2
			AND ((entity_kind = $3 AND entity_id = ANY($5)) OR (entity_kind = $4 AND entity_id = ANY($6)))`,
		threadID, stored.projectionEpoch, threadfold.KindItem, threadfold.KindCommand,
		pq.Array(required.ItemIDs), pq.Array(required.CommandIDs))
	if err != nil {
		return prior, err
	}
	defer rows.Close()
	for rows.Next() {
		row := threadfold.EntityRow{ThreadID: threadID, ProjectionEpoch: stored.projectionEpoch}
		var state []byte
		var references threadfold.ReferenceColumns
		if err := rows.Scan(&row.EntityKind, &row.EntityID, &row.Cursor, &row.RevisionCursor, &row.Pending, &row.TurnID,
			&state, &references.Text, &references.Arguments, &references.Output, &references.Input); err != nil {
			return prior, err
		}
		row.State = state
		if err := row.SetReferences(references); err != nil {
			return prior, err
		}
		switch row.EntityKind {
		case threadfold.KindItem:
			item, err := row.Item()
			if err != nil {
				return prior, err
			}
			prior.Items[row.EntityID] = &item
		case threadfold.KindCommand:
			command, err := row.CommandSummary()
			if err != nil {
				return prior, err
			}
			prior.Commands[row.EntityID] = &command
		}
	}
	if err := rows.Err(); err != nil {
		return prior, err
	}
	var completed []threadfold.PayloadRef
	for _, completion := range required.Completed {
		item := prior.Items[completion.ItemID]
		if item == nil {
			continue
		}
		reference, err := item.Payload(completion.Field)
		if err != nil {
			return prior, err
		}
		if reference != nil && !slices.Contains(completed, *reference) {
			completed = append(completed, *reference)
		}
	}
	prior.Bodies, err = payloadBodies(ctx, tx, threadID, completed)
	return prior, err
}

// payloadBodies returns each reference's whole text: its generation's chunks, as far as the reference spans them.
func payloadBodies(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, references []threadfold.PayloadRef) (map[threadfold.PayloadRef]string, error) {
	bodies := map[threadfold.PayloadRef]string{}
	extents, err := payloadExtents(ctx, tx, threadID, references)
	if err != nil {
		return nil, err
	}
	for _, reference := range references {
		extent, ok := extents[reference]
		if !ok {
			return nil, fmt.Errorf("%w: payload %s of %s has no manifest", ErrInconsistentFold, reference.Field, reference.OwnerID)
		}
		rows, err := tx.QueryContext(ctx, `SELECT text FROM thread_payload_chunk
			WHERE thread_id = $1 AND projection_epoch = $2 AND owner_cursor = $3 AND owner_id = $4 AND field = $5
				AND generation = $6 AND chunk_index < $7 ORDER BY chunk_index`,
			threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID, reference.Field,
			reference.Generation, extent)
		if err != nil {
			return nil, err
		}
		var body string
		for rows.Next() {
			var encoded []byte
			var chunk string
			if err := rows.Scan(&encoded); err != nil {
				rows.Close()
				return nil, err
			}
			if err := json.Unmarshal(encoded, &chunk); err != nil {
				rows.Close()
				return nil, err
			}
			body += chunk
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return nil, err
		}
		bodies[reference] = body
	}
	return bodies, nil
}

// payloadExtents returns how many chunks each stored reference spans, from the manifest of the revision it names.
func payloadExtents(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, references []threadfold.PayloadRef) (map[threadfold.PayloadRef]uint64, error) {
	extents := map[threadfold.PayloadRef]uint64{}
	if len(references) == 0 {
		return extents, nil
	}
	var epochs, owners, fields []string
	var ownerCursors, revisionCursors, generations []int64
	for _, reference := range references {
		epochs = append(epochs, reference.ProjectionEpoch)
		ownerCursors = append(ownerCursors, int64(reference.OwnerCursor))
		owners = append(owners, reference.OwnerID)
		fields = append(fields, string(reference.Field))
		revisionCursors = append(revisionCursors, int64(reference.RevisionCursor))
		generations = append(generations, int64(reference.Generation))
	}
	rows, err := tx.QueryContext(ctx, `SELECT manifest.projection_epoch, manifest.owner_cursor, manifest.owner_id, manifest.field,
			manifest.revision_cursor, manifest.generation, manifest.chunk_count
		FROM thread_payload_manifest manifest
		JOIN unnest($2::text[], $3::bigint[], $4::text[], $5::text[], $6::bigint[], $7::bigint[])
			AS wanted(projection_epoch, owner_cursor, owner_id, field, revision_cursor, generation)
			ON (manifest.projection_epoch, manifest.owner_cursor, manifest.owner_id, manifest.field,
				manifest.revision_cursor, manifest.generation)
			= (wanted.projection_epoch, wanted.owner_cursor, wanted.owner_id, wanted.field, wanted.revision_cursor, wanted.generation)
		WHERE manifest.thread_id = $1`, threadID, pq.Array(epochs), pq.Array(ownerCursors), pq.Array(owners),
		pq.Array(fields), pq.Array(revisionCursors), pq.Array(generations))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var reference threadfold.PayloadRef
		var count uint64
		if err := rows.Scan(&reference.ProjectionEpoch, &reference.OwnerCursor, &reference.OwnerID, &reference.Field,
			&reference.RevisionCursor, &reference.Generation, &count); err != nil {
			return nil, err
		}
		extents[reference] = count
	}
	return extents, rows.Err()
}

func nextEntityIndex(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, projectionEpoch string) (uint64, error) {
	var highest sql.NullInt64
	if err := tx.QueryRowContext(ctx, "SELECT max(entity_index) FROM thread_entity WHERE thread_id = $1 AND projection_epoch = $2",
		threadID, projectionEpoch).Scan(&highest); err != nil {
		return 0, err
	}
	if !highest.Valid {
		return 0, nil
	}
	return uint64(highest.Int64) + 1, nil
}
