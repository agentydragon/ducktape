package threadfold

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// A synchronized entity row is what Thread readers see. Its columns are the fields of EntityRow; the
// JSON shapes below are the contract of the `state` and reference columns, which the browser reads
// through Electric, so they are written exactly as the readers expect them: snake_case keys, a null for
// every absent value, and counters carried as decimal strings so readers never lose precision.

// Reference locates a payload body and says how many of its generation's chunks the revision spans.
type Reference struct {
	ProjectionEpoch string       `json:"projection_epoch"`
	OwnerCursor     uint64       `json:"owner_cursor,string"`
	OwnerID         string       `json:"owner_id"`
	Field           PayloadField `json:"field"`
	RevisionCursor  uint64       `json:"revision_cursor,string"`
	Generation      uint64       `json:"generation,string"`
	// ChunkCount is how many of the generation's chunks this revision spans; a reader renders at most these.
	ChunkCount uint64 `json:"chunk_count,string"`
}

// Ref is the payload reference a stored Reference names.
func (r Reference) Ref() PayloadRef {
	return PayloadRef{r.ProjectionEpoch, r.OwnerCursor, r.OwnerID, r.Field, r.RevisionCursor, r.Generation}
}

type OperationalStatus string

const (
	StatusActive OperationalStatus = "active"
	StatusEnded  OperationalStatus = "ended"
	StatusFailed OperationalStatus = "failed"
)

type FeedError struct {
	Cursor  *uint64 `json:"cursor,string"`
	Message string  `json:"message"`
}

// Operational is the feed lifecycle state, versioned independently of the runner cursor.
type Operational struct {
	Status             OperationalStatus `json:"status"`
	LastVerifiedCursor uint64            `json:"last_verified_cursor,string"`
	FeedError          *FeedError        `json:"feed_error"`
}

type StoredControls struct {
	AppliedModel           *string `json:"applied_model"`
	AppliedReasoningEffort *string `json:"applied_reasoning_effort"`
	ActiveTurnID           *string `json:"active_turn_id"`
	HarnessState           *string `json:"harness_state"`
}

// StoredViewState is the `state` of the one view-state row of a Thread.
type StoredViewState struct {
	Controls    StoredControls `json:"controls"`
	Operational Operational    `json:"operational"`
}

type StoredItemState struct {
	Kind          int32   `json:"kind"`
	ToolName      string  `json:"tool_name"`
	Completion    *string `json:"completion"`
	ToolSucceeded *bool   `json:"tool_succeeded"`
	Recovery      *int32  `json:"recovery"`
	// RecoveryReason is set when Recovery is.
	RecoveryReason *string `json:"recovery_reason"`
}

type StoredConfirmedInputState struct {
	HarnessMessageID string   `json:"harness_message_id"`
	OriginCommandIDs []string `json:"origin_command_ids"`
}

type StoredLifecycleState struct {
	Observation string `json:"observation"`
	// Event is the Event as protobuf JSON.
	Event json.RawMessage `json:"event"`
}

type StoredCommandState struct {
	Operation      string         `json:"operation"`
	Outcome        CommandOutcome `json:"outcome"`
	OutcomeCursor  *uint64        `json:"outcome_cursor,string"`
	OutcomeReason  *string        `json:"outcome_reason"`
	RequestedValue *string        `json:"requested_value"`
}

// EntityRow is one row of the Thread entity table, minus the dense entity index the recorder assigns.
type EntityRow struct {
	ThreadID        uuid.UUID       `json:"thread_id"`
	ProjectionEpoch string          `json:"projection_epoch"`
	EntityKind      EntityKind      `json:"entity_kind"`
	EntityID        string          `json:"entity_id"`
	Cursor          uint64          `json:"cursor,string"`
	RevisionCursor  uint64          `json:"revision_cursor,string"`
	Pending         bool            `json:"pending"`
	TurnID          *string         `json:"turn_id"`
	State           json.RawMessage `json:"state"`
	TextRef         *Reference      `json:"text_ref"`
	ArgumentsRef    *Reference      `json:"arguments_ref"`
	OutputRef       *Reference      `json:"output_ref"`
	InputRef        *Reference      `json:"input_ref"`
}

// PayloadReferences returns every payload reference a batch's rows store.
func PayloadReferences(result ProjectionBatch) []PayloadRef {
	var references []PayloadRef
	for _, item := range result.ItemUpserts {
		for _, reference := range []*PayloadRef{item.Text, item.Arguments, item.Output} {
			if reference != nil {
				references = append(references, *reference)
			}
		}
	}
	for _, input := range result.ConfirmedInputUpserts {
		references = append(references, input.Text)
	}
	for _, command := range result.CommandUpserts {
		if command.Input != nil {
			references = append(references, *command.Input)
		}
	}
	return references
}

// OrderedEntityRows returns a batch's rows in thread order, which is the order they are numbered. The
// recorder numbers rows in the order it inserts them, so kind and identity break a tie between rows at one
// cursor: a batch numbers the same rows the same way however it was assembled.
//
// extents holds, for every reference PayloadReferences returns, how many chunks the stored revision spans.
// A nil operational is a healthy feed verified through the batch's cursor.
func OrderedEntityRows(
	threadID uuid.UUID, result ProjectionBatch, extents map[PayloadRef]uint64, operational *Operational,
) ([]EntityRow, error) {
	builder := rowBuilder{threadID: threadID, extents: extents}
	rows := []EntityRow{builder.viewState(result.State, operational)}
	for _, item := range result.ItemUpserts {
		rows = append(rows, builder.item(item))
	}
	for _, input := range result.ConfirmedInputUpserts {
		rows = append(rows, builder.confirmedInput(input))
	}
	for _, segment := range result.LifecycleUpserts {
		rows = append(rows, builder.lifecycle(segment))
	}
	for _, command := range result.CommandUpserts {
		rows = append(rows, builder.command(command))
	}
	if builder.err != nil {
		return nil, builder.err
	}
	slices.SortFunc(rows, func(a, b EntityRow) int {
		return cmp.Or(cmp.Compare(a.Cursor, b.Cursor), cmp.Compare(a.EntityKind, b.EntityKind), cmp.Compare(a.EntityID, b.EntityID))
	})
	return rows, nil
}

type rowBuilder struct {
	threadID uuid.UUID
	extents  map[PayloadRef]uint64
	err      error
}

func (b *rowBuilder) state(value any) json.RawMessage {
	encoded, err := json.Marshal(value)
	if err != nil {
		b.err = errors.Join(b.err, err)
	}
	return encoded
}

func (b *rowBuilder) reference(reference *PayloadRef) *Reference {
	if reference == nil {
		return nil
	}
	chunks, ok := b.extents[*reference]
	if !ok {
		b.err = errors.Join(b.err, fmt.Errorf("no chunk count for payload %s of %s at revision %d",
			reference.Field, reference.OwnerID, reference.RevisionCursor))
	}
	return &Reference{
		reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID, reference.Field,
		reference.RevisionCursor, reference.Generation, chunks,
	}
}

func (b *rowBuilder) viewState(state ViewState, operational *Operational) EntityRow {
	through := state.Position.ThroughCursor
	if operational == nil {
		operational = &Operational{Status: StatusActive, LastVerifiedCursor: through}
	}
	return EntityRow{
		ThreadID:        b.threadID,
		ProjectionEpoch: state.Position.ProjectionEpoch,
		EntityKind:      KindViewState,
		EntityID:        "current",
		Cursor:          through,
		RevisionCursor:  through,
		State: b.state(StoredViewState{
			Controls: StoredControls{
				AppliedModel:           state.Controls.AppliedModel,
				AppliedReasoningEffort: state.Controls.AppliedReasoningEffort,
				ActiveTurnID:           state.Controls.ActiveTurnID,
				HarnessState:           state.Controls.HarnessState,
			},
			Operational: *operational,
		}),
	}
}

func (b *rowBuilder) item(item Item) EntityRow {
	stored := StoredItemState{Kind: int32(item.Kind), ToolName: item.ToolName, RecoveryReason: item.RecoveryReason}
	switch completion := item.Completion.(type) {
	case TextCompletion:
		stored.Completion = proto.String("text")
	case ToolCompletion:
		stored.Completion, stored.ToolSucceeded = proto.String("tool"), proto.Bool(completion.Succeeded)
	}
	if item.Recovery != nil {
		stored.Recovery = proto.Int32(int32(*item.Recovery))
	}
	return EntityRow{
		ThreadID:        b.threadID,
		ProjectionEpoch: item.ProjectionEpoch,
		EntityKind:      KindItem,
		EntityID:        item.ItemID,
		Cursor:          item.Cursor,
		RevisionCursor:  item.RevisionCursor,
		TurnID:          item.TurnID,
		State:           b.state(stored),
		TextRef:         b.reference(item.Text),
		ArgumentsRef:    b.reference(item.Arguments),
		OutputRef:       b.reference(item.Output),
	}
}

func (b *rowBuilder) confirmedInput(input ConfirmedInput) EntityRow {
	return EntityRow{
		ThreadID:        b.threadID,
		ProjectionEpoch: input.ProjectionEpoch,
		EntityKind:      KindConfirmedInput,
		EntityID:        fmt.Sprint(input.Cursor),
		Cursor:          input.Cursor,
		RevisionCursor:  input.RevisionCursor,
		TurnID:          input.TurnID,
		State: b.state(StoredConfirmedInputState{
			HarnessMessageID: input.HarnessMessageID, OriginCommandIDs: slices.Clone(input.OriginCommandIDs),
		}),
		InputRef: b.reference(&input.Text),
	}
}

func eventJSON(event *eventpb.Event) (json.RawMessage, error) {
	encoded, err := protojson.Marshal(event)
	if err != nil {
		return nil, err
	}
	// protojson output is deliberately unstable in its whitespace.
	var compact bytes.Buffer
	if err := json.Compact(&compact, encoded); err != nil {
		return nil, err
	}
	return compact.Bytes(), nil
}

func (b *rowBuilder) lifecycle(segment LifecycleSegment) EntityRow {
	event, err := eventJSON(segment.Event)
	b.err = errors.Join(b.err, err)
	return EntityRow{
		ThreadID:        b.threadID,
		ProjectionEpoch: segment.ProjectionEpoch,
		EntityKind:      KindLifecycle,
		EntityID:        fmt.Sprint(segment.Cursor),
		Cursor:          segment.Cursor,
		RevisionCursor:  segment.RevisionCursor,
		State:           b.state(StoredLifecycleState{Observation: segment.Observation, Event: event}),
	}
}

func (b *rowBuilder) command(command CommandSummary) EntityRow {
	revision := command.AdmissionCursor
	if command.OutcomeCursor != nil && *command.OutcomeCursor != 0 {
		revision = *command.OutcomeCursor
	}
	return EntityRow{
		ThreadID:        b.threadID,
		ProjectionEpoch: command.ProjectionEpoch,
		EntityKind:      KindCommand,
		EntityID:        command.CommandID,
		Cursor:          command.AdmissionCursor,
		RevisionCursor:  revision,
		Pending:         command.Outcome == OutcomePending,
		State: b.state(StoredCommandState{
			Operation:      command.Operation,
			Outcome:        command.Outcome,
			OutcomeCursor:  command.OutcomeCursor,
			OutcomeReason:  command.OutcomeReason,
			RequestedValue: command.RequestedValue,
		}),
		InputRef: b.reference(command.Input),
	}
}

func decodeStrict(data []byte, into any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("trailing data after JSON value")
	}
	return nil
}

// ReadViewState parses the `state` of a Thread's view-state row.
func ReadViewState(state json.RawMessage) (StoredViewState, error) {
	var view StoredViewState
	if err := decodeStrict(state, &view); err != nil {
		return StoredViewState{}, fmt.Errorf("view-state row: %w", err)
	}
	if !slices.Contains([]OperationalStatus{StatusActive, StatusEnded, StatusFailed}, view.Operational.Status) {
		return StoredViewState{}, fmt.Errorf("view-state row: unknown status %q", view.Operational.Status)
	}
	return view, nil
}

func (r Reference) validate() error {
	if !slices.Contains(payloadFields, r.Field) {
		return fmt.Errorf("unknown payload field %q", r.Field)
	}
	return nil
}

// ParseReference parses the JSON of a stored reference column.
func ParseReference(data []byte) (Reference, error) {
	var reference Reference
	if err := decodeStrict(data, &reference); err != nil {
		return Reference{}, err
	}
	return reference, reference.validate()
}

// ReferenceColumns are an entity row's reference columns as stored: JSON, or nil when absent.
type ReferenceColumns struct{ Text, Arguments, Output, Input []byte }

// SetReferences parses the stored reference columns into the row.
func (r *EntityRow) SetReferences(columns ReferenceColumns) error {
	for _, column := range []struct {
		stored []byte
		parsed **Reference
	}{{columns.Text, &r.TextRef}, {columns.Arguments, &r.ArgumentsRef}, {columns.Output, &r.OutputRef}, {columns.Input, &r.InputRef}} {
		if column.stored == nil {
			continue
		}
		reference, err := ParseReference(column.stored)
		if err != nil {
			return fmt.Errorf("%s row %s: %w", r.EntityKind, r.EntityID, err)
		}
		*column.parsed = &reference
	}
	return nil
}

func (r *Reference) ref() (*PayloadRef, error) {
	if r == nil {
		return nil, nil
	}
	if err := r.validate(); err != nil {
		return nil, err
	}
	ref := r.Ref()
	return &ref, nil
}

// Item parses the row of an item the fold reads back as prior state.
func (r EntityRow) Item() (Item, error) {
	var state StoredItemState
	if err := decodeStrict(r.State, &state); err != nil {
		return Item{}, fmt.Errorf("item row %s: %w", r.EntityID, err)
	}
	item := Item{
		ProjectionEpoch: r.ProjectionEpoch,
		ItemID:          r.EntityID,
		Cursor:          r.Cursor,
		RevisionCursor:  r.RevisionCursor,
		Kind:            eventpb.ItemKind(state.Kind),
		ToolName:        state.ToolName,
		TurnID:          r.TurnID,
		RecoveryReason:  state.RecoveryReason,
	}
	for _, held := range []struct {
		stored *Reference
		into   **PayloadRef
	}{{r.TextRef, &item.Text}, {r.ArgumentsRef, &item.Arguments}, {r.OutputRef, &item.Output}} {
		reference, err := held.stored.ref()
		if err != nil {
			return Item{}, fmt.Errorf("item row %s: %w", r.EntityID, err)
		}
		*held.into = reference
	}
	if state.Recovery != nil {
		recovery := eventpb.RecoveryDisposition(*state.Recovery)
		item.Recovery = &recovery
	}
	switch {
	case state.Completion == nil && state.ToolSucceeded == nil:
	case state.Completion != nil && *state.Completion == "text" && state.ToolSucceeded == nil:
		item.Completion = TextCompletion{}
	case state.Completion != nil && *state.Completion == "tool" && state.ToolSucceeded != nil:
		item.Completion = ToolCompletion{Succeeded: *state.ToolSucceeded}
	default:
		return Item{}, fmt.Errorf("item row %s: invalid item completion", r.EntityID)
	}
	return item, nil
}

// Validate reports whether the row is a published view of its kind: its state has the kind's shape.
func (r EntityRow) Validate() error {
	switch r.EntityKind {
	case KindViewState:
		_, err := ReadViewState(r.State)
		return err
	case KindItem:
		_, err := r.Item()
		return err
	case KindConfirmedInput:
		if err := decodeStrict(r.State, &StoredConfirmedInputState{}); err != nil {
			return fmt.Errorf("confirmed-input row %s: %w", r.EntityID, err)
		}
		return nil
	case KindLifecycle:
		if err := decodeStrict(r.State, &StoredLifecycleState{}); err != nil {
			return fmt.Errorf("lifecycle row %s: %w", r.EntityID, err)
		}
		return nil
	case KindCommand:
		_, err := r.CommandSummary()
		return err
	}
	return fmt.Errorf("unknown entity kind %q", r.EntityKind)
}

// CommandSummary parses the row of a command the fold reads back as prior state.
func (r EntityRow) CommandSummary() (CommandSummary, error) {
	var state StoredCommandState
	if err := decodeStrict(r.State, &state); err != nil {
		return CommandSummary{}, fmt.Errorf("command row %s: %w", r.EntityID, err)
	}
	if !slices.Contains([]CommandOutcome{OutcomePending, OutcomeEffected, OutcomeFailed, OutcomeNoop}, state.Outcome) {
		return CommandSummary{}, fmt.Errorf("command row %s: unknown outcome %q", r.EntityID, state.Outcome)
	}
	input, err := r.InputRef.ref()
	if err != nil {
		return CommandSummary{}, fmt.Errorf("command row %s: %w", r.EntityID, err)
	}
	return CommandSummary{
		ProjectionEpoch: r.ProjectionEpoch,
		CommandID:       r.EntityID,
		AdmissionCursor: r.Cursor,
		Operation:       state.Operation,
		Outcome:         state.Outcome,
		OutcomeCursor:   state.OutcomeCursor,
		OutcomeReason:   state.OutcomeReason,
		Input:           input,
		RequestedValue:  state.RequestedValue,
	}, nil
}
