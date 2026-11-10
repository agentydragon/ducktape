// Package threadfold folds a Session's runner Event log into the rows its Thread readers
// synchronize: items, confirmed inputs, lifecycle segments and command summaries, plus the payload
// bodies they reference.
//
// The fold is pure. It touches no database; it takes the rows a batch needs as input and returns
// typed upserts and payload write intents for a transactional storage worker.
package threadfold

import (
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

// EntityKind is the kind column of a synchronized entity row.
type EntityKind string

const (
	KindViewState      EntityKind = "view_state"
	KindItem           EntityKind = "item"
	KindConfirmedInput EntityKind = "confirmed_input"
	KindLifecycle      EntityKind = "lifecycle"
	KindCommand        EntityKind = "command"
)

// ObservationNotUnderstoodError means the fold does not model an observation the runner recorded.
type ObservationNotUnderstoodError struct {
	Cursor      uint64
	Observation string
}

func (e *ObservationNotUnderstoodError) Error() string {
	return fmt.Sprintf("uninterpreted semantic observation %q", e.Observation)
}

// ContractError means the batch, or the rows preloaded for it, break the fold's contract: a caller bug,
// not a newer runner.
type ContractError struct {
	Cursor  uint64
	Message string
}

func (e *ContractError) Error() string { return e.Message }

var errBatchMismatch = errors.New("batch does not match projection")

type Position struct {
	SourceID        string
	ProjectionEpoch string
	ThroughCursor   uint64
}

type PayloadRef struct {
	ProjectionEpoch string
	OwnerCursor     uint64
	OwnerID         string
	Field           historypb.ThreadPayloadField
	RevisionCursor  uint64
	Generation      uint64
}

// PayloadOwner is the identity shared by an item field, command input, or confirmed input.
type PayloadOwner struct {
	ProjectionEpoch string
	Cursor          uint64
	OwnerID         string
	RevisionCursor  uint64
}

// Completion is how an item finished: a TextCompletion or a ToolCompletion. Nil while it has not.
type Completion interface{ completion() }

type TextCompletion struct{}

type ToolCompletion struct{ Succeeded bool }

func (TextCompletion) completion() {}
func (ToolCompletion) completion() {}

type Item struct {
	ProjectionEpoch string
	ItemID          string
	Cursor          uint64
	RevisionCursor  uint64
	Kind            eventpb.ItemKind
	ToolName        string
	TurnID          *string
	Text            *PayloadRef
	Arguments       *PayloadRef
	Output          *PayloadRef
	Completion      Completion
	Recovery        *eventpb.RecoveryDisposition
	RecoveryReason  *string
}

// Payload returns the reference an item holds in field, or nil.
func (i Item) Payload(field historypb.ThreadPayloadField) (*PayloadRef, error) {
	switch field {
	case historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_TEXT:
		return i.Text, nil
	case historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_ARGUMENTS:
		return i.Arguments, nil
	case historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_OUTPUT:
		return i.Output, nil
	}
	return nil, fmt.Errorf("field %s does not belong to a thread item", field)
}

type ConfirmedInput struct {
	ProjectionEpoch  string
	Cursor           uint64
	RevisionCursor   uint64
	HarnessMessageID string
	OriginCommandIDs []string
	TurnID           *string
	Text             PayloadRef
}

type LifecycleSegment struct {
	ProjectionEpoch string
	Cursor          uint64
	RevisionCursor  uint64
	Event           *eventpb.Event
}

type CommandSummary struct {
	ProjectionEpoch string
	CommandID       string
	AdmissionCursor uint64
	Operation       commandpb.Operation
	Outcome         historypb.ThreadCommandOutcome
	OutcomeCursor   *uint64
	OutcomeReason   *string
	Input           *PayloadRef
	RequestedValue  *string
}

type Controls struct {
	AppliedModel           *string
	ActiveTurnID           *string
	HarnessState           *string
	AppliedReasoningEffort *string
}

type ViewState struct {
	Position Position
	Controls Controls
}

type EvidenceAssociation struct {
	ProjectionEpoch   string
	EntityCursor      uint64
	ObservationCursor uint64
	SourceSequences   []uint64
}

// PayloadWrite is an intent to store a body: an AppendPayload or a ReplacePayload.
type PayloadWrite interface {
	// Ref is the revision this write creates.
	Ref() PayloadRef
	// Fragment is the text this write adds (append) or holds (replace).
	Fragment() string
}

// AppendPayload adds Text to the body Base names, within Base's generation. A nil Base starts a body.
type AppendPayload struct {
	Base      *PayloadRef
	Reference PayloadRef
	Text      string
}

// ReplacePayload starts a new generation whose whole body is Text.
type ReplacePayload struct {
	Reference PayloadRef
	Text      string
}

func (w AppendPayload) Ref() PayloadRef   { return w.Reference }
func (w AppendPayload) Fragment() string  { return w.Text }
func (w ReplacePayload) Ref() PayloadRef  { return w.Reference }
func (w ReplacePayload) Fragment() string { return w.Text }

// PriorEntities holds rows preloaded only for TouchedKeys. A key that is absent from a map was not looked
// up; one present with a nil value was, and proved absent.
type PriorEntities struct {
	Items    map[string]*Item
	Commands map[string]*CommandSummary
	// Bodies holds the whole text of each preloaded item field in TouchedKeys.Completed.
	Bodies map[PayloadRef]string
}

type EventBatch struct {
	SourceID    string
	AfterCursor uint64
	Entries     []*eventlogpb.EventEntry
	// Settled holds the cursor ranges history no longer stores, settled deltas and removed records; the
	// entries skip them.
	Settled []*historypb.CursorRange
}

// ItemField is an item field an authoritative value completes.
type ItemField struct {
	ItemID string
	Field  historypb.ThreadPayloadField
}

// TouchedKeys holds the rows a worker preloads before folding a batch, each sorted.
type TouchedKeys struct {
	ItemIDs    []string
	CommandIDs []string
	// Completed holds the item fields an authoritative value completes, which the fold compares with the
	// stored body.
	Completed []ItemField
}

type ProjectionBatch struct {
	State                 ViewState
	ItemUpserts           []Item
	ConfirmedInputUpserts []ConfirmedInput
	LifecycleUpserts      []LifecycleSegment
	CommandUpserts        []CommandSummary
	EvidenceUpserts       []EvidenceAssociation
	PayloadWrites         []PayloadWrite
}
