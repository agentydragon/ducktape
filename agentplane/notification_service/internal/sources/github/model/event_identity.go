package model

import "github.com/google/uuid"

// GitHubEvent identifies one signed webhook delivery.
type GitHubEvent struct {
	AppID        int64
	DeliveryID   uuid.UUID
	RepositoryID int64
	Event        EventName
	Action       *string
}

// GitHubObservationEvent identifies an Agentplane-generated observation of current GitHub state.
// Sequence is a durable, generated-event sequence within its event category, not a GitHub delivery ID.
type GitHubObservationEvent struct {
	AppID        int64
	RepositoryID int64
	Event        EventName
	Sequence     int64
}

// GitHubMergeabilityEvent identifies one generated pull-request mergeability observation.
// Sequence is a durable, globally increasing generated-event sequence, not a GitHub delivery ID.
type GitHubMergeabilityEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Sequence          int64
}

// GitHubMergeabilitySnapshotEvent identifies a current conflicted snapshot sent to a joining subscriber.
// Version changes when the snapshot's relevant state changes.
type GitHubMergeabilitySnapshotEvent struct {
	AppID             int64
	RepositoryID      int64
	PullRequestNumber int64
	Version           int64
}
