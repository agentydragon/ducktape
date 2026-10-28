package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
)

// These codecs are test-only fixtures for JSON written by the retired Python/legacy Go storage paths. Current
// service code stores relational identities and typed subscription creation instead.
func marshalHistoricalEventIdentity(event model.EventIdentity) ([]byte, error) {
	switch event := event.(type) {
	case model.ActionsEvent:
		return json.Marshal(struct {
			Provider  string    `json:"provider"`
			RequestID uuid.UUID `json:"request_id"`
			Sequence  int64     `json:"sequence"`
		}{"actions", event.RequestID, event.Sequence})
	case model.GitHubEvent:
		return json.Marshal(struct {
			Provider     string          `json:"provider"`
			AppID        int64           `json:"app_id"`
			DeliveryID   uuid.UUID       `json:"delivery_id"`
			RepositoryID int64           `json:"repository_id"`
			Event        model.EventName `json:"event"`
			Action       *string         `json:"action"`
		}{"github", event.AppID, event.DeliveryID, event.RepositoryID, event.Event, event.Action})
	case model.GitHubObservationEvent:
		return json.Marshal(struct {
			Provider     string          `json:"provider"`
			AppID        int64           `json:"app_id"`
			RepositoryID int64           `json:"repository_id"`
			Event        model.EventName `json:"event"`
			Sequence     int64           `json:"sequence"`
		}{"github_observation", event.AppID, event.RepositoryID, event.Event, event.Sequence})
	case model.GitHubMergeabilityEvent:
		return json.Marshal(struct {
			Provider          string `json:"provider"`
			AppID             int64  `json:"app_id"`
			RepositoryID      int64  `json:"repository_id"`
			PullRequestNumber int64  `json:"pull_request_number"`
			Sequence          int64  `json:"sequence"`
		}{"github_mergeability", event.AppID, event.RepositoryID, event.PullRequestNumber, event.Sequence})
	case model.GitHubMergeabilitySnapshotEvent:
		return json.Marshal(struct {
			Provider          string `json:"provider"`
			AppID             int64  `json:"app_id"`
			RepositoryID      int64  `json:"repository_id"`
			PullRequestNumber int64  `json:"pull_request_number"`
			SnapshotVersion   int64  `json:"snapshot_version"`
		}{"github_mergeability_snapshot", event.AppID, event.RepositoryID, event.PullRequestNumber, event.Version})
	case model.MessageEvent:
		return json.Marshal(struct {
			Provider  string               `json:"provider"`
			MessageID uuid.UUID            `json:"message_id"`
			Sender    model.ServiceAccount `json:"sender"`
		}{"message", event.MessageID, event.Sender})
	default:
		return nil, fmt.Errorf("unsupported historical event identity %T", event)
	}
}

func decodeHistoricalEventIdentity(data []byte) (model.EventIdentity, error) {
	var head struct {
		Provider string `json:"provider"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Provider {
	case "actions":
		var wire struct {
			Provider  string     `json:"provider"`
			RequestID *uuid.UUID `json:"request_id"`
			Sequence  *int64     `json:"sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("request_id", wire.RequestID != nil); err != nil {
			return nil, err
		}
		if err := required("sequence", wire.Sequence != nil); err != nil {
			return nil, err
		}
		return model.ActionsEvent{RequestID: *wire.RequestID, Sequence: *wire.Sequence}, nil
	case "github":
		var wire struct {
			Provider     string          `json:"provider"`
			AppID        *int64          `json:"app_id"`
			DeliveryID   *uuid.UUID      `json:"delivery_id"`
			RepositoryID *int64          `json:"repository_id"`
			Event        model.EventName `json:"event"`
			Action       *string         `json:"action"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("app_id", wire.AppID != nil); err != nil {
			return nil, err
		}
		if err := required("delivery_id", wire.DeliveryID != nil); err != nil {
			return nil, err
		}
		if err := required("repository_id", wire.RepositoryID != nil); err != nil {
			return nil, err
		}
		if !model.IsGitHubWebhookEvent(wire.Event) {
			return nil, fmt.Errorf("unknown GitHub event %q", wire.Event)
		}
		return model.GitHubEvent{
			AppID: *wire.AppID, DeliveryID: *wire.DeliveryID, RepositoryID: *wire.RepositoryID,
			Event: wire.Event, Action: wire.Action,
		}, nil
	case "github_observation":
		var wire struct {
			Provider     string           `json:"provider"`
			AppID        *int64           `json:"app_id"`
			RepositoryID *int64           `json:"repository_id"`
			Event        *model.EventName `json:"event"`
			Sequence     *int64           `json:"sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("repository_id", wire.RepositoryID != nil),
			required("event", wire.Event != nil),
			required("sequence", wire.Sequence != nil),
		); err != nil {
			return nil, err
		}
		if *wire.AppID <= 0 || *wire.RepositoryID <= 0 || *wire.Sequence <= 0 {
			return nil, errors.New("app_id, repository_id and sequence must be positive")
		}
		if *wire.Event != model.EventCheckRun && *wire.Event != model.EventStatus && *wire.Event != model.EventPullRequestReview && *wire.Event != model.EventPullRequest {
			return nil, fmt.Errorf("unsupported GitHub observation event %q", *wire.Event)
		}
		return model.GitHubObservationEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID, Event: *wire.Event, Sequence: *wire.Sequence,
		}, nil
	case "github_mergeability":
		var wire struct {
			Provider          string `json:"provider"`
			AppID             *int64 `json:"app_id"`
			RepositoryID      *int64 `json:"repository_id"`
			PullRequestNumber *int64 `json:"pull_request_number"`
			Sequence          *int64 `json:"sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("repository_id", wire.RepositoryID != nil),
			required("pull_request_number", wire.PullRequestNumber != nil),
			required("sequence", wire.Sequence != nil),
		); err != nil {
			return nil, err
		}
		return model.GitHubMergeabilityEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID,
			PullRequestNumber: *wire.PullRequestNumber, Sequence: *wire.Sequence,
		}, nil
	case "github_mergeability_snapshot":
		var wire struct {
			Provider          string `json:"provider"`
			AppID             *int64 `json:"app_id"`
			RepositoryID      *int64 `json:"repository_id"`
			PullRequestNumber *int64 `json:"pull_request_number"`
			SnapshotVersion   *int64 `json:"snapshot_version"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("repository_id", wire.RepositoryID != nil),
			required("pull_request_number", wire.PullRequestNumber != nil),
			required("snapshot_version", wire.SnapshotVersion != nil),
		); err != nil {
			return nil, err
		}
		return model.GitHubMergeabilitySnapshotEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID,
			PullRequestNumber: *wire.PullRequestNumber, Version: *wire.SnapshotVersion,
		}, nil
	case "message":
		var wire struct {
			Provider  string                `json:"provider"`
			MessageID *uuid.UUID            `json:"message_id"`
			Sender    *model.ServiceAccount `json:"sender"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("message_id", wire.MessageID != nil); err != nil {
			return nil, err
		}
		if err := required("sender", wire.Sender != nil); err != nil {
			return nil, err
		}
		return model.MessageEvent{MessageID: *wire.MessageID, Sender: *wire.Sender}, nil
	default:
		return nil, fmt.Errorf("unknown event provider %q", head.Provider)
	}
}

func TestHistoricalGitHubObservationIdentityRemainsReadable(t *testing.T) {
	const wire = `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":3}`
	got, err := decodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	want := model.GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: model.EventCheckRun, Sequence: 3}
	if got != want {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	encoded, err := marshalHistoricalEventIdentity(got)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
}

func decodeHistoricalSubscription(data []byte) (model.Subscribe, error) {
	var wire struct {
		DestinationRef *model.DestinationRef `json:"destination_ref"`
		SessionID      *string               `json:"session_id"`
		IdempotencyKey *string               `json:"idempotency_key"`
		Source         json.RawMessage       `json:"source"`
		LifetimeDays   *int64                `json:"lifetime_days"`
	}
	if err := decode(data, &wire); err != nil {
		return model.Subscribe{}, err
	}
	if err := errors.Join(
		required("destination_ref", wire.DestinationRef != nil),
		required("session_id", wire.SessionID != nil),
		required("idempotency_key", wire.IdempotencyKey != nil),
		required("source", len(wire.Source) > 0),
	); err != nil {
		return model.Subscribe{}, err
	}
	source, err := decodeHistoricalSource(wire.Source)
	if err != nil {
		return model.Subscribe{}, err
	}
	creation := model.Subscribe{
		DestinationRef: *wire.DestinationRef, SessionID: *wire.SessionID,
		IdempotencyKey: *wire.IdempotencyKey, Source: source, LifetimeDays: 7,
	}
	if wire.LifetimeDays != nil {
		creation.LifetimeDays = *wire.LifetimeDays
	}
	return creation, nil
}

func decodeHistoricalSource(data []byte) (model.Source, error) {
	var head struct {
		Provider string `json:"provider"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Provider {
	case "actions":
		var wire struct {
			Provider      string     `json:"provider"`
			RequestID     *uuid.UUID `json:"request_id"`
			AfterSequence *int64     `json:"after_sequence"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("request_id", wire.RequestID != nil); err != nil {
			return nil, err
		}
		source := model.ActionsSource{RequestID: *wire.RequestID}
		if wire.AfterSequence != nil {
			source.AfterSequence = *wire.AfterSequence
		}
		return source, nil
	case "github":
		var wire struct {
			Provider   string                  `json:"provider"`
			Repository *string                 `json:"repository"`
			Subject    json.RawMessage         `json:"subject"`
			Events     []historicalEventFilter `json:"events"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("repository", wire.Repository != nil),
			required("subject", len(wire.Subject) > 0),
		); err != nil {
			return nil, err
		}
		subject, err := decodeHistoricalSubject(wire.Subject)
		if err != nil {
			return nil, err
		}
		source := model.GitHubSource{Repository: *wire.Repository, Subject: subject}
		if wire.Events != nil {
			source.Events = make([]model.EventFilter, len(wire.Events))
			for i, filter := range wire.Events {
				source.Events[i] = model.EventFilter{Event: filter.Event, Actions: filter.Actions}
			}
		}
		return source, nil
	default:
		return nil, fmt.Errorf("unknown source provider %q", head.Provider)
	}
}

type historicalEventFilter struct {
	Event   model.EventName `json:"event"`
	Actions []string        `json:"actions"`
}

func decodeHistoricalSubject(data []byte) (model.GitHubSubject, error) {
	var head struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, err
	}
	switch head.Kind {
	case "pull_request", "issue":
		var wire struct {
			Kind   string `json:"kind"`
			Number *int64 `json:"number"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("number", wire.Number != nil); err != nil {
			return nil, err
		}
		if head.Kind == "issue" {
			return model.IssueSubject{Number: *wire.Number}, nil
		}
		return model.PullRequestSubject{Number: *wire.Number}, nil
	case "branch":
		var wire struct {
			Kind string  `json:"kind"`
			Name *string `json:"name"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("name", wire.Name != nil); err != nil {
			return nil, err
		}
		return model.BranchSubject{Name: *wire.Name}, nil
	case "commit":
		var wire struct {
			Kind string  `json:"kind"`
			SHA  *string `json:"sha"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := required("sha", wire.SHA != nil); err != nil {
			return nil, err
		}
		return model.CommitSubject{SHA: *wire.SHA}, nil
	default:
		return nil, fmt.Errorf("unknown subject kind %q", head.Kind)
	}
}
