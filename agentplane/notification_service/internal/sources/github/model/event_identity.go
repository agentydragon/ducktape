package model

import "github.com/google/uuid"

// WebhookEvent identifies one signed webhook delivery.
type WebhookEvent struct {
	AppID        int64
	DeliveryID   uuid.UUID
	RepositoryID int64
	Event        EventName
	Action       *string
}

// ObservationEvent identifies an Agentplane-generated observation of current GitHub state.
// Sequence is a durable, generated-event sequence within its event category, not a GitHub delivery ID.
type ObservationEvent struct {
	AppID        int64
	RepositoryID int64
	Event        EventName
	Sequence     int64
}

// MergeabilityEvent identifies one generated pull-request mergeability observation.
// Sequence is a durable, globally increasing generated-event sequence, not a GitHub delivery ID.
type MergeabilityEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Sequence          int64
}

// MergeabilitySnapshotEvent identifies a current conflicted snapshot sent to a joining subscriber.
// Version changes when the snapshot's relevant state changes.
type MergeabilitySnapshotEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Version           int64
}

// EventIdentity is the closed set of GitHub-owned notification identities.
type EventIdentity interface{ isEventIdentity() }

func (WebhookEvent) isEventIdentity()              {}
func (ObservationEvent) isEventIdentity()          {}
func (MergeabilityEvent) isEventIdentity()         {}
func (MergeabilitySnapshotEvent) isEventIdentity() {}
