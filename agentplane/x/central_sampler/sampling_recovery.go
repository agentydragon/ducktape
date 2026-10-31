package centralsampler

import (
	"context"
	"database/sql"
	"fmt"
	"slices"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func samplingReconciliation(ctx context.Context, tx *sql.Tx, session *Session, intentID string) (*eventpb.Event, error) {
	report := &eventpb.ConversationReconciled{}
	linkedItems := make(map[uint64][]string)
	absent := make(map[string]bool)
	var sources []uint64
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
			// Scan only the pending request's suffix. Source links distinguish its items
			// from any unrelated observations interleaved after dispatch.
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
				report.TurnId = boundary.GetParentId()
				ids := make([]string, 0, len(absent))
				for id := range absent {
					ids = append(ids, id)
				}
				slices.Sort(ids)
				for _, id := range ids {
					report.Items = append(report.Items, &eventpb.ItemRecovery{
						ItemId: id, Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT,
					})
				}
				sources = append(sources, sequence)
				slices.Sort(sources)
				return &eventpb.Event{
					SourceSequences: sources,
					Observation:     &eventpb.Event_ConversationReconciled{ConversationReconciled: report},
				}, nil
			}
		}
	}
	return nil, fmt.Errorf("sampling intent %s has no dispatch evidence", intentID)
}

func recoveryEventsBefore(ctx context.Context, tx *sql.Tx, sessionID string, before uint64) ([]*eventlogpb.EventEntry, error) {
	// Bound each query even when the Session has a long archive: closing a PostgreSQL
	// result early must not drain all earlier entries from the connection.
	rows, err := tx.QueryContext(ctx, `SELECT entry FROM sampler_event
		WHERE session_id=$1 AND sequence<$2 ORDER BY sequence DESC LIMIT 256`, sessionID, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []*eventlogpb.EventEntry
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			return nil, err
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(wire, entry); err != nil {
			return nil, err
		}
		entries = append(entries, entry)
	}
	return entries, rows.Err()
}
