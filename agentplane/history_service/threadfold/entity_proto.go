package threadfold

import (
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// StoredEntityKind maps the public key kind to the indexed database value.
func StoredEntityKind(kind historypb.ThreadEntityKind) (EntityKind, error) {
	switch kind {
	case historypb.ThreadEntityKind_THREAD_ENTITY_KIND_VIEW_STATE:
		return KindViewState, nil
	case historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM:
		return KindItem, nil
	case historypb.ThreadEntityKind_THREAD_ENTITY_KIND_CONFIRMED_INPUT:
		return KindConfirmedInput, nil
	case historypb.ThreadEntityKind_THREAD_ENTITY_KIND_LIFECYCLE:
		return KindLifecycle, nil
	case historypb.ThreadEntityKind_THREAD_ENTITY_KIND_COMMAND:
		return KindCommand, nil
	default:
		return "", fmt.Errorf("unknown entity kind %d", kind)
	}
}

func EntityKindOf(e *historypb.ThreadEntity) (EntityKind, error) {
	switch e.Entity.(type) {
	case *historypb.ThreadEntity_ViewState:
		return KindViewState, nil
	case *historypb.ThreadEntity_Item:
		return KindItem, nil
	case *historypb.ThreadEntity_ConfirmedInput:
		return KindConfirmedInput, nil
	case *historypb.ThreadEntity_Lifecycle:
		return KindLifecycle, nil
	case *historypb.ThreadEntity_Command:
		return KindCommand, nil
	default:
		return "", fmt.Errorf("missing entity variant")
	}
}

// EntityState returns the canonical variant message stored as ProtoJSON.
func EntityState(e *historypb.ThreadEntity) (proto.Message, error) {
	switch v := e.Entity.(type) {
	case *historypb.ThreadEntity_ViewState:
		return v.ViewState, nil
	case *historypb.ThreadEntity_Item:
		return v.Item, nil
	case *historypb.ThreadEntity_ConfirmedInput:
		return v.ConfirmedInput, nil
	case *historypb.ThreadEntity_Lifecycle:
		return v.Lifecycle, nil
	case *historypb.ThreadEntity_Command:
		return v.Command, nil
	default:
		return nil, fmt.Errorf("missing entity variant")
	}
}

// ParseEntity reads indexed columns and their canonical variant ProtoJSON.
func ParseEntity(e *historypb.ThreadEntity, kind EntityKind, pending bool, state []byte) error {
	var value proto.Message
	switch kind {
	case KindViewState:
		value = &historypb.ThreadViewState{}
		e.Entity = &historypb.ThreadEntity_ViewState{ViewState: value.(*historypb.ThreadViewState)}
	case KindItem:
		value = &historypb.ThreadItem{}
		e.Entity = &historypb.ThreadEntity_Item{Item: value.(*historypb.ThreadItem)}
	case KindConfirmedInput:
		value = &historypb.ThreadConfirmedInput{}
		e.Entity = &historypb.ThreadEntity_ConfirmedInput{ConfirmedInput: value.(*historypb.ThreadConfirmedInput)}
	case KindLifecycle:
		value = &historypb.ThreadLifecycle{}
		e.Entity = &historypb.ThreadEntity_Lifecycle{Lifecycle: value.(*historypb.ThreadLifecycle)}
	case KindCommand:
		value = &historypb.ThreadCommand{}
		e.Entity = &historypb.ThreadEntity_Command{Command: value.(*historypb.ThreadCommand)}
	default:
		return fmt.Errorf("unknown entity kind %q", kind)
	}
	if err := protojson.Unmarshal(state, value); err != nil {
		return fmt.Errorf("%s row %s: %w", kind, e.EntityId, err)
	}
	return ValidateEntity(e, pending)
}

func validatePayloadField(field historypb.ThreadPayloadField) error {
	if _, ok := historypb.ThreadPayloadField_name[int32(field)]; !ok || field == historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_UNSPECIFIED {
		return fmt.Errorf("unknown payload field %d", field)
	}
	return nil
}

func payloadRef(r *historypb.ThreadPayloadReference) (*PayloadRef, error) {
	if r == nil {
		return nil, nil
	}
	if err := validatePayloadField(r.Field); err != nil {
		return nil, err
	}
	return &PayloadRef{r.ProjectionEpoch, r.OwnerCursor, r.OwnerId, r.Field, r.RevisionCursor, r.Generation}, nil
}

func validateOutcome(outcome historypb.ThreadCommandOutcome) error {
	switch outcome {
	case historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_PENDING, historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_EFFECTED, historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_FAILED, historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_NOOP:
		return nil
	default:
		return fmt.Errorf("unknown command outcome %d", outcome)
	}
}

// ValidateOperational checks the generated feed state before it is stored or used.
func ValidateOperational(o *historypb.ThreadOperational) error {
	if o == nil {
		return fmt.Errorf("missing operational state")
	}
	switch o.Status {
	case historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ACTIVE, historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ENDED, historypb.ThreadFeedStatus_THREAD_FEED_STATUS_FAILED:
		return nil
	default:
		return fmt.Errorf("unknown feed status %d", o.Status)
	}
}

func ReadViewState(data []byte) (*historypb.ThreadViewState, error) {
	v := &historypb.ThreadViewState{}
	if err := protojson.Unmarshal(data, v); err != nil {
		return nil, err
	}
	if v.Controls == nil {
		return nil, fmt.Errorf("missing controls")
	}
	if err := ValidateOperational(v.Operational); err != nil {
		return nil, err
	}
	return v, nil
}

func ItemFromEntity(e *historypb.ThreadEntity) (Item, error) {
	v := e.GetItem()
	if v == nil {
		return Item{}, fmt.Errorf("not an item")
	}
	item := Item{ProjectionEpoch: e.ProjectionEpoch, ItemID: e.EntityId, Cursor: e.Cursor, RevisionCursor: e.RevisionCursor, Kind: v.Kind, ToolName: v.ToolName, TurnID: v.TurnId, Recovery: v.Recovery, RecoveryReason: v.RecoveryReason}
	var err error
	if item.Text, err = payloadRef(v.TextRef); err != nil {
		return Item{}, err
	}
	if item.Arguments, err = payloadRef(v.ArgumentsRef); err != nil {
		return Item{}, err
	}
	if item.Output, err = payloadRef(v.OutputRef); err != nil {
		return Item{}, err
	}
	switch c := v.Completion.(type) {
	case *historypb.ThreadItem_Text:
		item.Completion = TextCompletion{}
	case *historypb.ThreadItem_Tool:
		if c.Tool == nil {
			return Item{}, fmt.Errorf("missing tool completion")
		}
		item.Completion = ToolCompletion{Succeeded: c.Tool.Succeeded}
	}
	return item, nil
}

func CommandFromEntity(e *historypb.ThreadEntity) (CommandSummary, error) {
	v := e.GetCommand()
	if v == nil {
		return CommandSummary{}, fmt.Errorf("not a command")
	}
	if err := validateOutcome(v.Outcome); err != nil {
		return CommandSummary{}, err
	}
	input, err := payloadRef(v.InputRef)
	if err != nil {
		return CommandSummary{}, err
	}
	return CommandSummary{ProjectionEpoch: e.ProjectionEpoch, CommandID: e.EntityId, AdmissionCursor: e.Cursor, Operation: v.Operation, Outcome: v.Outcome, OutcomeCursor: v.OutcomeCursor, OutcomeReason: v.OutcomeReason, Input: input, RequestedValue: v.RequestedValue}, nil
}

// ValidateEntity checks the index and reference invariants shared by storage readers.
func ValidateEntity(e *historypb.ThreadEntity, pending bool) error {
	kind, err := EntityKindOf(e)
	if err != nil {
		return err
	}
	if kind != KindCommand && pending {
		return fmt.Errorf("%s row %s cannot be pending", kind, e.EntityId)
	}
	var refs []struct {
		r     *historypb.ThreadPayloadReference
		field historypb.ThreadPayloadField
		owner string
	}
	switch kind {
	case KindViewState:
		v := e.GetViewState()
		if v == nil || v.Controls == nil {
			return fmt.Errorf("missing view state or controls")
		}
		if err := ValidateOperational(v.Operational); err != nil {
			return err
		}
	case KindItem:
		v := e.GetItem()
		if v == nil {
			return fmt.Errorf("missing item")
		}
		refs = append(refs, struct {
			r     *historypb.ThreadPayloadReference
			field historypb.ThreadPayloadField
			owner string
		}{v.TextRef, historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_TEXT, e.EntityId}, struct {
			r     *historypb.ThreadPayloadReference
			field historypb.ThreadPayloadField
			owner string
		}{v.ArgumentsRef, historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_ARGUMENTS, e.EntityId}, struct {
			r     *historypb.ThreadPayloadReference
			field historypb.ThreadPayloadField
			owner string
		}{v.OutputRef, historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_OUTPUT, e.EntityId})
	case KindConfirmedInput:
		v := e.GetConfirmedInput()
		if v == nil {
			return fmt.Errorf("missing input")
		}
		owner := v.HarnessMessageId
		if owner == "" {
			owner = fmt.Sprintf("confirmed:%d", e.Cursor)
		}
		refs = append(refs, struct {
			r     *historypb.ThreadPayloadReference
			field historypb.ThreadPayloadField
			owner string
		}{v.InputRef, historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_CONFIRMED_INPUT, owner})
	case KindLifecycle:
		v := e.GetLifecycle()
		if v == nil || v.Event == nil || v.Event.ProtoReflect().WhichOneof(v.Event.ProtoReflect().Descriptor().Oneofs().ByName("observation")) == nil {
			return fmt.Errorf("missing lifecycle observation")
		}
	case KindCommand:
		v := e.GetCommand()
		if v == nil {
			return fmt.Errorf("missing command")
		}
		if err := validateOutcome(v.Outcome); err != nil {
			return err
		}
		if pending != (v.Outcome == historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_PENDING) {
			return fmt.Errorf("command pending column disagrees with outcome")
		}
		refs = append(refs, struct {
			r     *historypb.ThreadPayloadReference
			field historypb.ThreadPayloadField
			owner string
		}{v.InputRef, historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_COMMAND_INPUT, e.EntityId})
	}
	for _, held := range refs {
		r := held.r
		if r == nil {
			continue
		}
		if r.Field != held.field || r.ProjectionEpoch != e.ProjectionEpoch || r.OwnerCursor != e.Cursor || r.OwnerId != held.owner || r.Generation < e.Cursor || r.Generation > r.RevisionCursor || r.RevisionCursor > e.RevisionCursor || (kind != KindItem && r.RevisionCursor != e.Cursor) {
			return fmt.Errorf("%s row %s has an invalid payload reference", kind, e.EntityId)
		}
	}
	return nil
}
