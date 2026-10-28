package model

import (
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
