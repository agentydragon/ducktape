package model

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	"github.com/google/uuid"
)

// EventIdentity names an entry's event within its source, which is how a redelivery is told from a new event:
// an ActionsEvent, a GitHubEvent, a GitHubObservationEvent, a GitHubMergeabilityEvent, a
// GitHubMergeabilitySnapshotEvent or a MessageEvent.
type EventIdentity interface{ isEventIdentity() }

type ActionsEvent struct {
	RequestID uuid.UUID
	Sequence  int64
}

type GitHubEvent struct {
	AppID        int64
	DeliveryID   uuid.UUID
	RepositoryID int64
	Event        EventName
	Action       *string
}

// GitHubObservationEvent is an Agentplane-generated observation of current GitHub state.
// Sequence is a durable, generated-event sequence within its event category, not a GitHub delivery ID.
type GitHubObservationEvent struct {
	AppID        int64
	RepositoryID int64
	Event        EventName
	Sequence     int64
}

// GitHubMergeabilityEvent is one Agentplane-generated observation of a pull request's mergeability.
// Sequence is a durable, globally increasing generated-event sequence, not a GitHub delivery ID.
type GitHubMergeabilityEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Sequence          int64
}

// GitHubMergeabilitySnapshotEvent identifies a current conflicted snapshot sent to a joining subscriber. Version
// changes when the snapshot's relevant state changes.
type GitHubMergeabilitySnapshotEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Version           int64
}

// MessageEvent is a message a workload sent to the inbox, with the sender the service authenticated.
type MessageEvent struct {
	MessageID uuid.UUID
	Sender    ServiceAccount
}

func (ActionsEvent) isEventIdentity()                    {}
func (GitHubEvent) isEventIdentity()                     {}
func (GitHubObservationEvent) isEventIdentity()          {}
func (GitHubMergeabilityEvent) isEventIdentity()         {}
func (GitHubMergeabilitySnapshotEvent) isEventIdentity() {}
func (MessageEvent) isEventIdentity()                    {}
func (e ActionsEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider  string    `json:"provider"`
		RequestID uuid.UUID `json:"request_id"`
		Sequence  int64     `json:"sequence"`
	}{"actions", e.RequestID, e.Sequence})
}

func (e GitHubEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider     string    `json:"provider"`
		AppID        int64     `json:"app_id"`
		DeliveryID   uuid.UUID `json:"delivery_id"`
		RepositoryID int64     `json:"repository_id"`
		Event        EventName `json:"event"`
		Action       *string   `json:"action"`
	}{"github", e.AppID, e.DeliveryID, e.RepositoryID, e.Event, e.Action})
}

func (e GitHubObservationEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider     string    `json:"provider"`
		AppID        int64     `json:"app_id"`
		RepositoryID int64     `json:"repository_id"`
		Event        EventName `json:"event"`
		Sequence     int64     `json:"sequence"`
	}{"github_observation", e.AppID, e.RepositoryID, e.Event, e.Sequence})
}

func (e GitHubMergeabilityEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider          string `json:"provider"`
		AppID             int64  `json:"app_id"`
		RepositoryID      int64  `json:"repository_id"`
		PullRequestNumber int64  `json:"pull_request_number"`
		Sequence          int64  `json:"sequence"`
	}{"github_mergeability", e.AppID, e.RepositoryID, e.PullRequestNumber, e.Sequence})
}

func (e GitHubMergeabilitySnapshotEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider          string `json:"provider"`
		AppID             int64  `json:"app_id"`
		RepositoryID      int64  `json:"repository_id"`
		PullRequestNumber int64  `json:"pull_request_number"`
		SnapshotVersion   int64  `json:"snapshot_version"`
	}{"github_mergeability_snapshot", e.AppID, e.RepositoryID, e.PullRequestNumber, e.Version})
}

func (e MessageEvent) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Provider  string         `json:"provider"`
		MessageID uuid.UUID      `json:"message_id"`
		Sender    ServiceAccount `json:"sender"`
	}{"message", e.MessageID, e.Sender})
}

func decode(data []byte, v any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(v); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("trailing data after the JSON value")
	}
	return nil
}

// required reports a field that was absent or null.
func required(field string, present bool) error {
	if !present {
		return fmt.Errorf("%s is required", field)
	}
	return nil
}

// DecodeEventIdentity reads the JSON of an entry's `event` column.
func DecodeEventIdentity(data []byte) (EventIdentity, error) {
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
		if err := errors.Join(required("request_id", wire.RequestID != nil), required("sequence", wire.Sequence != nil)); err != nil {
			return nil, err
		}
		return ActionsEvent{RequestID: *wire.RequestID, Sequence: *wire.Sequence}, nil
	case "github":
		var wire struct {
			Provider     string     `json:"provider"`
			AppID        *int64     `json:"app_id"`
			DeliveryID   *uuid.UUID `json:"delivery_id"`
			RepositoryID *int64     `json:"repository_id"`
			Event        EventName  `json:"event"`
			Action       *string    `json:"action"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(
			required("app_id", wire.AppID != nil),
			required("delivery_id", wire.DeliveryID != nil),
			required("repository_id", wire.RepositoryID != nil),
		); err != nil {
			return nil, err
		}
		if !slices.Contains(knownEvents, wire.Event) {
			return nil, fmt.Errorf("unknown GitHub event %q", wire.Event)
		}
		return GitHubEvent{
			AppID: *wire.AppID, DeliveryID: *wire.DeliveryID, RepositoryID: *wire.RepositoryID,
			Event: wire.Event, Action: wire.Action,
		}, nil
	case "github_observation":
		var wire struct {
			Provider     string     `json:"provider"`
			AppID        *int64     `json:"app_id"`
			RepositoryID *int64     `json:"repository_id"`
			Event        *EventName `json:"event"`
			Sequence     *int64     `json:"sequence"`
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
		if *wire.Event != EventCheckRun && *wire.Event != EventStatus && *wire.Event != EventPullRequestReview && *wire.Event != EventPullRequest {
			return nil, fmt.Errorf("unsupported GitHub observation event %q", *wire.Event)
		}
		return GitHubObservationEvent{
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
		return GitHubMergeabilityEvent{
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
		return GitHubMergeabilitySnapshotEvent{
			AppID: *wire.AppID, RepositoryID: *wire.RepositoryID,
			PullRequestNumber: *wire.PullRequestNumber, Version: *wire.SnapshotVersion,
		}, nil
	case "message":
		var wire struct {
			Provider  string          `json:"provider"`
			MessageID *uuid.UUID      `json:"message_id"`
			Sender    *ServiceAccount `json:"sender"`
		}
		if err := decode(data, &wire); err != nil {
			return nil, err
		}
		if err := errors.Join(required("message_id", wire.MessageID != nil), required("sender", wire.Sender != nil)); err != nil {
			return nil, err
		}
		return MessageEvent{MessageID: *wire.MessageID, Sender: *wire.Sender}, nil
	default:
		return nil, fmt.Errorf("unknown event provider %q", head.Provider)
	}
}
