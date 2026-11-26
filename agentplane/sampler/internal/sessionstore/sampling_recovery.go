package sessionstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

func samplingReconciliation(ctx context.Context, tx *sql.Tx, session *Session, intentID string) (*eventpb.Event, error) {
	var turnID sql.NullString
	var dispatchSequence sql.NullInt64
	err := tx.QueryRowContext(ctx, `SELECT dispatch_turn_id,dispatch_sequence
		FROM sampler_intent WHERE id=$1 AND request->>'kind'='sampling'`, intentID).Scan(&turnID, &dispatchSequence)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if err == nil && turnID.Valid && dispatchSequence.Valid {
		return samplingReconciliationFromProjection(ctx, tx, intentID, turnID.String, uint64(dispatchSequence.Int64))
	}
	return samplingReconciliationFromJournal(ctx, tx, session, intentID)
}

func samplingReconciliationFromProjection(ctx context.Context, tx *sql.Tx, intentID, turnID string, dispatchSequence uint64) (*eventpb.Event, error) {
	sources := []uint64{dispatchSequence}
	rows, err := tx.QueryContext(ctx, `SELECT source_sequence FROM sampler_intent_source WHERE intent_id=$1`, intentID)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var source int64
		if err := rows.Scan(&source); err != nil {
			rows.Close()
			return nil, err
		}
		sources = append(sources, uint64(source))
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	slices.Sort(sources)
	sources = slices.Compact(sources)

	rows, err = tx.QueryContext(ctx, `SELECT item_id FROM sampler_intent_started_item WHERE intent_id=$1`, intentID)
	if err != nil {
		return nil, err
	}
	var itemIDs []string
	for rows.Next() {
		var itemID string
		if err := rows.Scan(&itemID); err != nil {
			rows.Close()
			return nil, err
		}
		itemIDs = append(itemIDs, itemID)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	slices.Sort(itemIDs)
	itemIDs = slices.Compact(itemIDs)

	return samplingReconciliationEvent(turnID, sources, itemIDs), nil
}

func samplingReconciliationFromJournal(ctx context.Context, tx *sql.Tx, session *Session, intentID string) (*eventpb.Event, error) {
	linkedItems := make(map[uint64][]string)
	absent := make(map[string]bool)
	var sources []uint64
	// Legacy or invalidated projections keep the original journal reconstruction path.
	before := session.Cursor + 1
	for before > 1 {
		entries, err := recoveryEventsBefore(ctx, tx, session.ID, before)
		if err != nil {
			return nil, err
		}
		if len(entries) == 0 {
			break
		}
		for _, entry := range entries {
			before = entry.GetCursor()
			event := entry.GetEvent()
			if started := event.GetItemStarted(); started != nil {
				for _, source := range event.GetSourceSequences() {
					linkedItems[source] = append(linkedItems[source], started.GetItemId())
				}
			}
			boundary := event.GetBoundaryRecord()
			if boundary == nil || boundary.GetCorrelationId() != intentID {
				continue
			}
			sequence := entry.GetOrigin().GetSequence()
			if items := linkedItems[sequence]; len(items) > 0 {
				for _, id := range items {
					absent[id] = true
				}
				sources = append(sources, sequence)
			}
			if boundary.GetBoundary() == string(IntentSampling) && boundary.GetDirection() == "dispatch" {
				// Reverse traversal sees the final fragment first, matching the dispatch
				// sequence retained by the indexed path.
				sources = append(sources, sequence)
				itemIDs := make([]string, 0, len(absent))
				for id := range absent {
					itemIDs = append(itemIDs, id)
				}
				slices.Sort(itemIDs)
				slices.Sort(sources)
				return samplingReconciliationEvent(boundary.GetParentId(), sources, itemIDs), nil
			}
		}
	}
	return nil, fmt.Errorf("sampling intent %s has no dispatch evidence", intentID)
}

func samplingReconciliationEvent(turnID string, sources []uint64, itemIDs []string) *eventpb.Event {
	report := &eventpb.ConversationReconciled{TurnId: turnID}
	for _, id := range itemIDs {
		report.Items = append(report.Items, &eventpb.ItemRecovery{
			ItemId: id, Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT,
		})
	}
	return &eventpb.Event{
		SourceSequences: sources,
		Observation:     &eventpb.Event_ConversationReconciled{ConversationReconciled: report},
	}
}

// recordSamplingStartedItems stores only source sequences that the ItemStarted observations
// actually reference. The item and its source links are written with those journal events.
func recordSamplingStartedItems(ctx context.Context, tx *sql.Tx, intentID string, sources []uint64, observations []*eventpb.Event) error {
	for _, observation := range observations {
		started := observation.GetItemStarted()
		if started == nil || len(sources) == 0 {
			continue
		}
		for _, source := range sources {
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent_source(intent_id,source_sequence)
				VALUES($1,$2) ON CONFLICT DO NOTHING`, intentID, int64(source)); err != nil {
				return err
			}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_intent_started_item(intent_id,item_id)
			VALUES($1,$2) ON CONFLICT DO NOTHING`, intentID, started.GetItemId()); err != nil {
			return err
		}
	}
	return nil
}

func persistSamplingDispatch(ctx context.Context, tx *sql.Tx, intentID string, turnID *string, sources []uint64) error {
	if turnID == nil || *turnID == "" || len(sources) == 0 {
		return nil
	}
	dispatchSequence := sources[len(sources)-1]
	result, err := tx.ExecContext(ctx, `UPDATE sampler_intent
		SET dispatch_turn_id=$2,dispatch_sequence=$3
		WHERE id=$1 AND request->>'kind'='sampling' AND result IS NULL`, intentID, *turnID, int64(dispatchSequence))
	if err != nil {
		return err
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != 1 {
		return fmt.Errorf("sampling dispatch projection for intent %s was not stored", intentID)
	}
	return nil
}

func deleteSamplingRecoveryProjection(ctx context.Context, tx *sql.Tx, intentID string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM sampler_intent_source WHERE intent_id=$1`, intentID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM sampler_intent_started_item WHERE intent_id=$1`, intentID); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET dispatch_turn_id=NULL,dispatch_sequence=NULL WHERE id=$1`, intentID)
	return err
}

func recoveryEventsBefore(ctx context.Context, tx *sql.Tx, sessionID string, before uint64) ([]*eventlogpb.EventEntry, error) {
	const pageSize = 256
	entries, err := readJournalRange(ctx, tx, sessionID, max(before, pageSize+1)-pageSize, before-1)
	slices.Reverse(entries)
	return entries, err
}
