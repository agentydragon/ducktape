package sourceadapter

import (
	"encoding/json"
	"fmt"
	"time"

	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// EntrySummary describes an entry the agent has not acknowledged without its payload.
type EntrySummary struct {
	Cursor           int64
	CreatedAt        time.Time
	Event            EventIdentity
	ActionState      *string
	GitHubConclusion *string
}

// SummarizeEntry describes an event identity and the few lifecycle fields of its payload that are safe to show.
func SummarizeEntry(cursor int64, event EventIdentity, payload []byte, createdAt time.Time) (EntrySummary, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(payload, &fields); err != nil {
		return EntrySummary{}, fmt.Errorf("entry %d payload: %w", cursor, err)
	}
	summary := EntrySummary{Cursor: cursor, CreatedAt: createdAt.UTC(), Event: event}
	switch event := event.(type) {
	case ActionsEvent:
		if stateData, ok := fields["state"]; ok {
			var state *string
			if err := json.Unmarshal(stateData, &state); err == nil {
				summary.ActionState = state
			}
		}
	case GitHubEvent:
		summary.GitHubConclusion = githubstoragecodec.SummaryConclusion(event.Event, fields)
	case GitHubObservationEvent, GitHubMergeabilityEvent, GitHubMergeabilitySnapshotEvent, MessageEvent:
	default:
		return EntrySummary{}, fmt.Errorf("entry %d: unknown event identity %T", cursor, event)
	}
	return summary, nil
}

// SummarizeStoredEntry describes an event from native storage without decoding content that cannot be summarized.
func SummarizeStoredEntry(cursor int64, event EventIdentity, githubPayload []byte, content model.EntryContentStorage, createdAt time.Time) (EntrySummary, error) {
	if _, ok := event.(MessageEvent); ok {
		return EntrySummary{Cursor: cursor, CreatedAt: createdAt.UTC(), Event: event}, nil
	}
	payload, err := EntryPayloadFromRead(event, githubPayload, content)
	if err != nil {
		return EntrySummary{}, fmt.Errorf("entry %d content: %w", cursor, err)
	}
	return SummarizeEntry(cursor, event, payload, createdAt)
}
