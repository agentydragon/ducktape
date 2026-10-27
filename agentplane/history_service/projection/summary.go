package projection

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

// ChannelSummaries is notified when the fold committed Session summaries.
const ChannelSummaries = "agentplane_history_summary_changes"

func notify(ctx context.Context, tx *sql.Tx, channel string) error {
	return postgres.Notify(ctx, tx, channel, "")
}

// summary is the event-derived sidebar metadata of a Thread. attached and end are JSON documents: NULL, or
// the Attached snapshot as protobuf JSON, and `{}` for a stream that ended cleanly or an object carrying the
// failure that ended it.
type summary struct {
	firstEventAt   sql.NullTime
	lastEventAt    sql.NullTime
	lastTurnStatus sql.NullInt64
	attached       json.RawMessage
	end            json.RawMessage
	idleSince      sql.NullTime
}

func loadSummary(ctx context.Context, tx *sql.Tx, threadID uuid.UUID) (*summary, error) {
	var found summary
	var attached, end []byte
	err := tx.QueryRowContext(ctx, `SELECT first_event_at, last_event_at, last_turn_status, attached, "end", idle_since
		FROM thread_history_summary WHERE thread_id = $1`, threadID).
		Scan(&found.firstEventAt, &found.lastEventAt, &found.lastTurnStatus, &attached, &end, &found.idleSince)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	found.attached, found.end = attached, end
	return &found, err
}

// store writes the columns the projection derives from events. last_model_activity_at has its own writer.
func (s *summary) store(ctx context.Context, tx *sql.Tx, threadID uuid.UUID) error {
	_, err := tx.ExecContext(ctx, `UPDATE thread_history_summary
		SET first_event_at = $2, last_event_at = $3, last_turn_status = $4, attached = $5::jsonb, "end" = $6::jsonb,
			idle_since = $7
		WHERE thread_id = $1`, threadID, s.firstEventAt, s.lastEventAt, s.lastTurnStatus, nullable(s.attached),
		nullable(s.end), s.idleSince)
	return err
}

func nullable(document json.RawMessage) any {
	if document == nil {
		return nil
	}
	return string(document)
}

// sameJSON reports whether two JSON documents hold the same value, whatever their formatting.
func sameJSON(a, b json.RawMessage) (bool, error) {
	if a == nil || b == nil {
		return a == nil && b == nil, nil
	}
	var left, right any
	if err := json.Unmarshal(a, &left); err != nil {
		return false, err
	}
	if err := json.Unmarshal(b, &right); err != nil {
		return false, err
	}
	return reflect.DeepEqual(left, right), nil
}

func sameTime(a, b sql.NullTime) bool {
	return a.Valid == b.Valid && a.Time.Equal(b.Time)
}

func (s *summary) sameAs(other *summary) (bool, error) {
	attached, err := sameJSON(s.attached, other.attached)
	if err != nil {
		return false, err
	}
	end, err := sameJSON(s.end, other.end)
	if err != nil {
		return false, err
	}
	return attached && end && sameTime(s.firstEventAt, other.firstEventAt) && sameTime(s.lastEventAt, other.lastEventAt) &&
		sameTime(s.idleSince, other.idleSince) && s.lastTurnStatus == other.lastTurnStatus, nil
}

// nextIdleSince is when a harness last became idle: from its start, or from the end of a turn, until the next
// turn starts or it stops.
func nextIdleSince(idleSince sql.NullTime, event *eventpb.Event) sql.NullTime {
	switch event.GetObservation().(type) {
	case *eventpb.Event_HarnessStarted, *eventpb.Event_TurnCompleted:
		return sql.NullTime{Time: eventTime(event), Valid: true}
	case *eventpb.Event_TurnStarted, *eventpb.Event_HarnessExited, *eventpb.Event_HarnessLost:
		return sql.NullTime{}
	}
	return idleSince
}

// isModelActivity is a conservative proxy for the Thread having used a model. It is not an observed provider
// request or evidence of a cache hit: tool results and harness bookkeeping can arrive much later than the
// request that caused them.
func isModelActivity(event *eventpb.Event) bool {
	switch observation := event.GetObservation().(type) {
	case *eventpb.Event_TextDelta:
		return observation.TextDelta.GetText() != ""
	case *eventpb.Event_ToolArgumentsDelta:
		return observation.ToolArgumentsDelta.GetPartialJson() != ""
	case *eventpb.Event_ToolArguments:
		return observation.ToolArguments.GetArgumentsJson() != ""
	case *eventpb.Event_ItemStarted:
		switch observation.ItemStarted.GetKind() {
		case eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, eventpb.ItemKind_ITEM_KIND_REASONING, eventpb.ItemKind_ITEM_KIND_TOOL_CALL:
			return true
		}
	case *eventpb.Event_ItemCompleted:
		return observation.ItemCompleted.GetText() != ""
	}
	return false
}

// recordModelActivity updates the Thread's last model activity from a newly admitted batch.
func recordModelActivity(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, entries []*eventlogpb.EventEntry) error {
	for index := len(entries) - 1; index >= 0; index-- {
		if isModelActivity(entries[index].GetEvent()) {
			_, err := tx.ExecContext(ctx, "UPDATE thread_history_summary SET last_model_activity_at = $2 WHERE thread_id = $1",
				threadID, eventTime(entries[index].GetEvent()))
			return err
		}
	}
	return nil
}
