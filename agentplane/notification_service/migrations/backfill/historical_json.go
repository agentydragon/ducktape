package backfill

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func DecodeHistoricalEventIdentity(data []byte) (model.EventIdentity, error) {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("request_id", wire.RequestID != nil); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("sequence", wire.Sequence != nil); err != nil {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("app_id", wire.AppID != nil); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("delivery_id", wire.DeliveryID != nil); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("repository_id", wire.RepositoryID != nil); err != nil {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			storagecodec.Required("app_id", wire.AppID != nil),
			storagecodec.Required("repository_id", wire.RepositoryID != nil),
			storagecodec.Required("event", wire.Event != nil),
			storagecodec.Required("sequence", wire.Sequence != nil),
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			storagecodec.Required("app_id", wire.AppID != nil),
			storagecodec.Required("repository_id", wire.RepositoryID != nil),
			storagecodec.Required("pull_request_number", wire.PullRequestNumber != nil),
			storagecodec.Required("sequence", wire.Sequence != nil),
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			storagecodec.Required("app_id", wire.AppID != nil),
			storagecodec.Required("repository_id", wire.RepositoryID != nil),
			storagecodec.Required("pull_request_number", wire.PullRequestNumber != nil),
			storagecodec.Required("snapshot_version", wire.SnapshotVersion != nil),
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("message_id", wire.MessageID != nil); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("sender", wire.Sender != nil); err != nil {
			return nil, err
		}
		return model.MessageEvent{MessageID: *wire.MessageID, Sender: *wire.Sender}, nil
	default:
		return nil, fmt.Errorf("unknown event provider %q", head.Provider)
	}
}

func DecodeHistoricalSubscription(data []byte) (model.Subscribe, error) {
	var wire struct {
		DestinationRef *model.DestinationRef `json:"destination_ref"`
		SessionID      *string               `json:"session_id"`
		IdempotencyKey *string               `json:"idempotency_key"`
		Source         json.RawMessage       `json:"source"`
		LifetimeDays   *int64                `json:"lifetime_days"`
	}
	if err := storagecodec.Decode(data, &wire); err != nil {
		return model.Subscribe{}, err
	}
	if err := errors.Join(
		storagecodec.Required("destination_ref", wire.DestinationRef != nil),
		storagecodec.Required("session_id", wire.SessionID != nil),
		storagecodec.Required("idempotency_key", wire.IdempotencyKey != nil),
		storagecodec.Required("source", len(wire.Source) > 0),
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("request_id", wire.RequestID != nil); err != nil {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			storagecodec.Required("repository", wire.Repository != nil),
			storagecodec.Required("subject", len(wire.Subject) > 0),
		); err != nil {
			return nil, err
		}
		subject, err := DecodeHistoricalSubject(wire.Subject)
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

func DecodeHistoricalSubject(data []byte) (model.GitHubSubject, error) {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("number", wire.Number != nil); err != nil {
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
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("name", wire.Name != nil); err != nil {
			return nil, err
		}
		return model.BranchSubject{Name: *wire.Name}, nil
	case "commit":
		var wire struct {
			Kind string  `json:"kind"`
			SHA  *string `json:"sha"`
		}
		if err := storagecodec.Decode(data, &wire); err != nil {
			return nil, err
		}
		if err := storagecodec.Required("sha", wire.SHA != nil); err != nil {
			return nil, err
		}
		return model.CommitSubject{SHA: *wire.SHA}, nil
	default:
		return nil, fmt.Errorf("unknown subject kind %q", head.Kind)
	}
}
