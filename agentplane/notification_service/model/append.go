package model

import (
	"encoding/json"
	"time"

	"github.com/google/uuid"
)

// AppendInput contains a permanent natural key and its expirable content.
type AppendInput struct {
	Key     EntryKey
	Payload EntryPayload
}

// EntryProvider is an existing relational identity discriminator.
type EntryProvider string

const (
	EntryProviderActions            EntryProvider = "actions"
	EntryProviderGitHub             EntryProvider = "github"
	EntryProviderGitHubObservation  EntryProvider = "github_observation"
	EntryProviderGitHubMergeability EntryProvider = "github_mergeability"
	EntryProviderMessage            EntryProvider = "message"
)

// EntryColumn is the closed vocabulary of existing natural-key columns.
type EntryColumn uint8

const (
	EntryActionsRequestID EntryColumn = iota
	EntryActionsSequence
	EntryGitHubAppID
	EntryGitHubDeliveryID
	EntryGitHubRepositoryID
	EntryGitHubEvent
	EntryGitHubAction
	EntryGitHubPullRequestNumber
	EntryGitHubMergeabilitySequence
	EntryGitHubSnapshotVersion
	EntryGitHubObservationSequence
	EntryMessageID
	EntryMessageSenderNamespace
	EntryMessageSenderName
)

// EntryValue preserves the distinction between SQL NULL and empty text.
type EntryValue interface{ isEntryValue() }

type (
	EntryUUID    uuid.UUID
	EntryInteger int64
	EntryText    string
	EntryNull    struct{}
)

func (EntryUUID) isEntryValue()    {}
func (EntryInteger) isEntryValue() {}
func (EntryText) isEntryValue()    {}
func (EntryNull) isEntryValue()    {}

type EntryKeyField struct {
	Column EntryColumn
	Value  EntryValue
}

// EntryKey is an ordered tuple of the source's existing natural-key fields.
type EntryKey struct {
	Provider EntryProvider
	Fields   []EntryKeyField
}

// EntryPayload keeps durable sender metadata separate from the deduplication key.
// JSON is populated only for content stored in the existing JSON column.
type EntryPayload struct {
	JSON           json.RawMessage
	Content        EntryContentStorage
	SenderIdentity *Identity
}

// EntryContentStorage is the neutral row shape for native Action, GitHub and Message content columns.
type EntryContentStorage struct {
	ActionState                         *int32
	ActionAt                            *time.Time
	ActionActorKind                     *string
	ActionActorNamespace                *string
	ActionActorName                     *string
	ActionActorIssuer                   *string
	ActionActorSubject                  *string
	ActionActorIdentityName             *string
	ActionActorIdentityUID              *string
	MergeabilityRepository              *string
	MergeabilityBaseRef                 *string
	MergeabilityHeadSHA                 *string
	MergeabilityBaseSHA                 *string
	MergeabilityPrevious                *string
	MergeabilityCurrent                 *string
	MergeabilityObservedAt              *time.Time
	MergeabilityObservedAtSubmicroNanos *int16
	MergeCommitSHA                      *string
	MergeCommitBaseSHA                  *string
	MergeCommitHeadSHA                  *string
	MessageBody                         *string
	MessageFromSession                  *string
}
