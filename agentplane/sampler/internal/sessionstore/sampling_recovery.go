package sessionstore

import (
	"context"
	"database/sql"
	"fmt"
	"slices"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

func samplingReconciliation(ctx context.Context, tx *sql.Tx, session *Session, intentID string) (*eventpb.Event, error) {
	report := &eventpb.ConversationReconciled{}
	linkedItems := make(map[uint64][]string)
	absent := make(map[string]bool)
	var sources []uint64
	// TODO: Index dispatch and source sequences by sampling intent, and item IDs
	// by source sequence, so recovery can fetch this intent's evidence without
	// scanning and decoding unrelated journal entries back to dispatch.
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
	const pageSize = 256
	entries, err := readJournalRange(ctx, tx, sessionID, max(before, pageSize+1)-pageSize, before-1)
	slices.Reverse(entries)
	return entries, err
}
