package threadfold

import (
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/history_service/settledcursors"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// silentObservations advance the projection without a row.
var silentObservations = map[string]bool{
	"native": true, "harness_stderr": true, "debug_checkpoint": true, "journal_reopened": true,
	"boundary_record": true,
}

// lifecycleObservations are kept whole as lifecycle segments.
var lifecycleObservations = map[string]bool{
	"turn_started": true, "turn_completed": true, "model_changed": true, "reasoning_effort_changed": true,
	"harness_started": true, "harness_exited": true, "harness_lost": true, "harness_launch_failed": true,
	"setup_started": true, "setup_output": true, "setup_finished": true, "setup_interrupted": true,
}

// Initial returns the view of a Session with no folded entry.
func Initial(sourceID, projectionEpoch string) (ViewState, error) {
	if sourceID == "" || projectionEpoch == "" {
		return ViewState{}, errors.New("projection source and epoch are required")
	}
	return ViewState{Position: Position{SourceID: sourceID, ProjectionEpoch: projectionEpoch}}, nil
}

// oneofName returns the name of the field set in the message's oneof, or "" when none is.
func oneofName(message proto.Message, oneof protoreflect.Name) string {
	reflected := message.ProtoReflect()
	if field := reflected.WhichOneof(reflected.Descriptor().Oneofs().ByName(oneof)); field != nil {
		return string(field.Name())
	}
	return ""
}

func observationName(event *eventpb.Event) string { return oneofName(event, "observation") }

// Touched discovers every row a worker needs preloaded before folding a batch.
func Touched(batch EventBatch) TouchedKeys {
	items := map[string]struct{}{}
	commands := map[string]struct{}{}
	completed := map[ItemField]struct{}{}
	for _, entry := range batch.Entries {
		event := entry.GetEvent()
		switch observationName(event) {
		case "item_started":
			items[event.GetItemStarted().GetItemId()] = struct{}{}
		case "text_delta":
			items[event.GetTextDelta().GetItemId()] = struct{}{}
		case "tool_arguments_delta":
			items[event.GetToolArgumentsDelta().GetItemId()] = struct{}{}
		case "tool_arguments":
			id := event.GetToolArguments().GetItemId()
			items[id] = struct{}{}
			completed[ItemField{id, FieldArguments}] = struct{}{}
		case "tool_output_delta":
			items[event.GetToolOutputDelta().GetItemId()] = struct{}{}
		case "item_completed":
			id := event.GetItemCompleted().GetItemId()
			items[id] = struct{}{}
			switch event.GetItemCompleted().GetOutcome().(type) {
			case *eventpb.ItemCompleted_Text:
				completed[ItemField{id, FieldText}] = struct{}{}
			case *eventpb.ItemCompleted_Tool:
				completed[ItemField{id, FieldOutput}] = struct{}{}
			}
		case "conversation_reconciled":
			for _, recovered := range event.GetConversationReconciled().GetItems() {
				items[recovered.GetItemId()] = struct{}{}
				if recovered.GetReplacement() != nil {
					for _, field := range []PayloadField{FieldText, FieldArguments, FieldOutput} {
						completed[ItemField{recovered.GetItemId(), field}] = struct{}{}
					}
				}
			}
		case "command_admitted":
			commands[event.GetCommandAdmitted().GetCommand().GetCommandId()] = struct{}{}
		case "command_failed":
			commands[event.GetCommandFailed().GetCommandId()] = struct{}{}
		case "command_noop":
			commands[event.GetCommandNoop().GetCommandId()] = struct{}{}
		case "harness_user_message_confirmed":
			for _, id := range event.GetHarnessUserMessageConfirmed().GetOriginCommandIds() {
				commands[id] = struct{}{}
			}
		case "turn_completed":
			commands[event.GetTurnCompleted().GetInterruptedByCommandId()] = struct{}{}
		case "model_changed":
			commands[event.GetModelChanged().GetCommandId()] = struct{}{}
		case "reasoning_effort_changed":
			commands[event.GetReasoningEffortChanged().GetCommandId()] = struct{}{}
		}
	}
	delete(items, "")
	delete(commands, "")
	keys := TouchedKeys{ItemIDs: slices.Sorted(maps.Keys(items)), CommandIDs: slices.Sorted(maps.Keys(commands))}
	for field := range completed {
		if field.ItemID != "" {
			keys.Completed = append(keys.Completed, field)
		}
	}
	slices.SortFunc(keys.Completed, func(a, b ItemField) int {
		if a.ItemID != b.ItemID {
			return cmp.Compare(a.ItemID, b.ItemID)
		}
		return cmp.Compare(a.Field, b.Field)
	})
	return keys
}

// ordered is a map that reports its values in first-insertion order, however often a key is overwritten.
type ordered[K comparable, V any] struct {
	keys   []K
	values map[K]V
}

func (o *ordered[K, V]) get(key K) (V, bool) {
	value, ok := o.values[key]
	return value, ok
}

func (o *ordered[K, V]) set(key K, value V) {
	if o.values == nil {
		o.values = map[K]V{}
	}
	if _, present := o.values[key]; !present {
		o.keys = append(o.keys, key)
	}
	o.values[key] = value
}

func (o *ordered[K, V]) all() []V {
	values := make([]V, 0, len(o.keys))
	for _, key := range o.keys {
		values = append(values, o.values[key])
	}
	return values
}

type folder struct {
	state          ViewState
	initialThrough uint64
	prior          PriorEntities
	items          ordered[string, Item]
	commands       ordered[string, CommandSummary]
	confirmed      ordered[uint64, ConfirmedInput]
	lifecycle      ordered[uint64, LifecycleSegment]
	evidence       []EvidenceAssociation
	writes         ordered[PayloadRef, PayloadWrite]
}

func itemOwner(item Item) PayloadOwner {
	return PayloadOwner{item.ProjectionEpoch, item.Cursor, item.ItemID, item.RevisionCursor}
}

func (f *folder) validateRef(reference PayloadRef, owner PayloadOwner, field PayloadField, preloaded bool) error {
	if reference.ProjectionEpoch != f.state.Position.ProjectionEpoch ||
		reference.OwnerCursor != owner.Cursor ||
		reference.OwnerID != owner.OwnerID ||
		reference.Field != field ||
		!(owner.Cursor <= reference.Generation && reference.Generation <= reference.RevisionCursor &&
			reference.RevisionCursor <= owner.RevisionCursor) ||
		(preloaded && reference.RevisionCursor > f.initialThrough) {
		return errors.New("payload reference does not belong to its owner revision")
	}
	return nil
}

func (f *folder) item(cursor uint64, itemID string) (Item, error) {
	if itemID == "" {
		return Item{}, errors.New("empty item identity")
	}
	if item, ok := f.items.get(itemID); ok {
		return item, nil
	}
	prior, looked := f.prior.Items[itemID]
	if !looked {
		return Item{}, fmt.Errorf("missing prior item lookup: %s", itemID)
	}
	var item Item
	if prior == nil {
		item = Item{
			ProjectionEpoch: f.state.Position.ProjectionEpoch,
			ItemID:          itemID,
			Cursor:          cursor,
			RevisionCursor:  cursor,
			TurnID:          f.state.Controls.ActiveTurnID,
		}
	} else {
		if prior.ProjectionEpoch != f.state.Position.ProjectionEpoch || prior.ItemID != itemID ||
			!(0 < prior.Cursor && prior.Cursor <= prior.RevisionCursor && prior.RevisionCursor <= f.initialThrough) {
			return Item{}, fmt.Errorf("invalid prior item: %s", itemID)
		}
		owner := itemOwner(*prior)
		for _, held := range []struct {
			field PayloadField
			ref   *PayloadRef
		}{{FieldText, prior.Text}, {FieldArguments, prior.Arguments}, {FieldOutput, prior.Output}} {
			if held.ref == nil {
				continue
			}
			if err := f.validateRef(*held.ref, owner, held.field, true); err != nil {
				return Item{}, err
			}
		}
		item = *prior
	}
	f.items.set(itemID, item)
	return item, nil
}

func (f *folder) saveItem(item Item, cursor uint64) Item {
	item.RevisionCursor = cursor
	f.items.set(item.ItemID, item)
	return item
}

func (f *folder) write(
	owner PayloadOwner, base *PayloadRef, cursor uint64, field PayloadField, text string, appending bool,
) (PayloadRef, error) {
	if base != nil {
		if err := f.validateRef(*base, owner, field, false); err != nil {
			return PayloadRef{}, err
		}
	}
	reference := PayloadRef{f.state.Position.ProjectionEpoch, owner.Cursor, owner.OwnerID, field, cursor, cursor}
	if appending && base != nil {
		reference.Generation = base.Generation
		if reference == *base {
			// The base is this cursor's own write, so only an empty append leaves it as it is.
			if text != "" {
				return PayloadRef{}, fmt.Errorf("payload %s of %s already written at revision %d", field, owner.OwnerID, cursor)
			}
			return reference, nil
		}
	}
	if appending {
		f.writes.set(reference, AppendPayload{Base: base, Reference: reference, Text: text})
	} else {
		f.writes.set(reference, ReplacePayload{Reference: reference, Text: text})
	}
	return reference, nil
}

// body returns a reference's whole text: this batch's appends back to its generation's first write, or the
// body preloaded for an earlier batch's revision.
func (f *folder) body(reference PayloadRef) (string, error) {
	var fragments []string
	for {
		write, written := f.writes.get(reference)
		appended, isAppend := write.(AppendPayload)
		if written && isAppend && appended.Base != nil {
			fragments = append(fragments, appended.Text)
			reference = *appended.Base
			continue
		}
		if written {
			fragments = append(fragments, write.Fragment())
		} else if prior, ok := f.prior.Bodies[reference]; ok {
			fragments = append(fragments, prior)
		} else {
			return "", fmt.Errorf("missing prior body lookup: %s %s", reference.OwnerID, reference.Field)
		}
		break
	}
	slices.Reverse(fragments)
	return strings.Join(fragments, ""), nil
}

func (f *folder) itemWrite(cursor uint64, itemID string, field PayloadField, text string, appending bool) (Item, error) {
	item, err := f.item(cursor, itemID)
	if err != nil {
		return Item{}, err
	}
	base, err := item.Payload(field)
	if err != nil {
		return Item{}, err
	}
	value, err := f.write(itemOwner(item), base, cursor, field, text, appending)
	if err != nil {
		return Item{}, err
	}
	switch field {
	case FieldText:
		item.Text = &value
	case FieldArguments:
		item.Arguments = &value
	case FieldOutput:
		item.Output = &value
	}
	return f.saveItem(item, cursor), nil
}

// complete makes an authoritative value the field's body, unless the body already holds exactly that text:
// then only the revision moves, so a reader holding the body has nothing to fetch.
func (f *folder) complete(cursor uint64, itemID string, field PayloadField, text string) (Item, error) {
	item, err := f.item(cursor, itemID)
	if err != nil {
		return Item{}, err
	}
	base, err := item.Payload(field)
	if err != nil {
		return Item{}, err
	}
	if base != nil {
		held, err := f.body(*base)
		if err != nil {
			return Item{}, err
		}
		if held == text {
			return f.itemWrite(cursor, itemID, field, "", true)
		}
	}
	return f.itemWrite(cursor, itemID, field, text, false)
}

func (f *folder) recordEvidence(entityCursor uint64, entry *eventlogpb.EventEntry) {
	f.evidence = append(f.evidence, EvidenceAssociation{
		ProjectionEpoch:   f.state.Position.ProjectionEpoch,
		EntityCursor:      entityCursor,
		ObservationCursor: entry.GetCursor(),
		SourceSequences:   slices.Clone(entry.GetEvent().GetSourceSequences()),
	})
}

func (f *folder) command(commandID string) (CommandSummary, error) {
	if summary, ok := f.commands.get(commandID); ok {
		return summary, nil
	}
	prior, looked := f.prior.Commands[commandID]
	if !looked {
		return CommandSummary{}, fmt.Errorf("missing prior command lookup: %s", commandID)
	}
	if prior == nil {
		return CommandSummary{}, fmt.Errorf("missing admitted command: %s", commandID)
	}
	if prior.ProjectionEpoch != f.state.Position.ProjectionEpoch || prior.CommandID != commandID ||
		!(0 < prior.AdmissionCursor && prior.AdmissionCursor <= f.initialThrough) {
		return CommandSummary{}, fmt.Errorf("invalid prior command: %s", commandID)
	}
	if reference := prior.Input; reference != nil {
		owner := PayloadOwner{prior.ProjectionEpoch, prior.AdmissionCursor, commandID, prior.AdmissionCursor}
		if f.validateRef(*reference, owner, FieldCommandInput, true) != nil ||
			reference.RevisionCursor != prior.AdmissionCursor {
			return CommandSummary{}, fmt.Errorf("invalid prior command input: %s", commandID)
		}
	}
	f.commands.set(commandID, *prior)
	return *prior, nil
}

func (f *folder) admit(cursor uint64, command *commandpb.Command) error {
	commandID := command.GetCommandId()
	prior, looked := f.prior.Commands[commandID]
	if commandID == "" || !looked {
		return fmt.Errorf("missing prior command lookup: %s", commandID)
	}
	if _, admitted := f.commands.get(commandID); admitted || prior != nil {
		return fmt.Errorf("duplicate command admission: %s", commandID)
	}
	operation := oneofName(command, "operation")
	if operation == "" {
		return &ObservationNotUnderstoodError{cursor, "command_admitted.operation"}
	}
	summary := CommandSummary{
		ProjectionEpoch: f.state.Position.ProjectionEpoch,
		CommandID:       commandID,
		AdmissionCursor: cursor,
		Operation:       operation,
		Outcome:         OutcomePending,
	}
	switch operation {
	case "change_model":
		summary.RequestedValue = proto.String(command.GetChangeModel().GetModel())
	case "change_reasoning_effort":
		summary.RequestedValue = proto.String(command.GetChangeReasoningEffort().GetEffort())
	case "submit_input":
		owner := PayloadOwner{summary.ProjectionEpoch, cursor, commandID, cursor}
		input, err := f.write(owner, nil, cursor, FieldCommandInput, command.GetSubmitInput().GetText(), false)
		if err != nil {
			return err
		}
		summary.Input = &input
	}
	f.commands.set(commandID, summary)
	return nil
}

func (f *folder) settle(entry *eventlogpb.EventEntry, commandID string, outcome CommandOutcome, reason *string) error {
	summary, err := f.command(commandID)
	if err != nil {
		return err
	}
	if summary.Outcome != OutcomePending {
		return fmt.Errorf("command already settled: %s", commandID)
	}
	cursor := entry.GetCursor()
	summary.Outcome, summary.OutcomeCursor, summary.OutcomeReason = outcome, &cursor, reason
	f.commands.set(commandID, summary)
	f.recordEvidence(summary.AdmissionCursor, entry)
	return nil
}

func (f *folder) recordLifecycle(entry *eventlogpb.EventEntry, observation string) error {
	cursor, event := entry.GetCursor(), entry.GetEvent()
	f.lifecycle.set(cursor, LifecycleSegment{
		ProjectionEpoch: f.state.Position.ProjectionEpoch,
		Cursor:          cursor,
		RevisionCursor:  cursor,
		Observation:     observation,
		Event:           proto.Clone(event).(*eventpb.Event),
	})
	controls := f.state.Controls
	switch observation {
	case "turn_started":
		started := event.GetTurnStarted()
		controls.ActiveTurnID = proto.String(started.GetTurnId())
		if started.GetModel() != "" {
			controls.AppliedModel = proto.String(started.GetModel())
		}
	case "turn_completed":
		completed := event.GetTurnCompleted()
		if completed.GetInterruptedByCommandId() != "" {
			if err := f.settle(entry, completed.GetInterruptedByCommandId(), OutcomeEffected, nil); err != nil {
				return err
			}
		}
		if controls.ActiveTurnID != nil && *controls.ActiveTurnID == completed.GetTurnId() {
			controls.ActiveTurnID = nil
		}
	case "model_changed":
		changed := event.GetModelChanged()
		if changed.GetCommandId() != "" {
			if err := f.settle(entry, changed.GetCommandId(), OutcomeEffected, nil); err != nil {
				return err
			}
		}
		controls.AppliedModel = proto.String(changed.GetModel())
	case "reasoning_effort_changed":
		changed := event.GetReasoningEffortChanged()
		if changed.GetCommandId() != "" {
			if err := f.settle(entry, changed.GetCommandId(), OutcomeEffected, nil); err != nil {
				return err
			}
		}
		controls.AppliedReasoningEffort = proto.String(changed.GetEffort())
	case "harness_started":
		controls.HarnessState = proto.String("running")
	case "harness_exited":
		controls.HarnessState, controls.ActiveTurnID = proto.String("stopped"), nil
	case "harness_lost":
		controls.HarnessState, controls.ActiveTurnID = proto.String("lost"), nil
	}
	f.state.Controls = controls
	return nil
}

func (f *folder) fold(entry *eventlogpb.EventEntry) error {
	cursor, event := entry.GetCursor(), entry.GetEvent()
	observation := observationName(event)
	switch observation {
	case "item_started":
		started := event.GetItemStarted()
		switch started.GetKind() {
		case eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, eventpb.ItemKind_ITEM_KIND_REASONING, eventpb.ItemKind_ITEM_KIND_TOOL_CALL:
		default:
			return &ObservationNotUnderstoodError{cursor, "item_started.kind"}
		}
		item, err := f.item(cursor, started.GetItemId())
		if err != nil {
			return err
		}
		item.Kind, item.ToolName = started.GetKind(), started.GetToolName()
		f.recordEvidence(f.saveItem(item, cursor).Cursor, entry)
	case "text_delta":
		delta := event.GetTextDelta()
		item, err := f.itemWrite(cursor, delta.GetItemId(), FieldText, delta.GetText(), true)
		if err != nil {
			return err
		}
		f.recordEvidence(item.Cursor, entry)
	case "tool_arguments_delta":
		delta := event.GetToolArgumentsDelta()
		item, err := f.itemWrite(cursor, delta.GetItemId(), FieldArguments, delta.GetPartialJson(), true)
		if err != nil {
			return err
		}
		f.recordEvidence(item.Cursor, entry)
	case "tool_arguments":
		arguments := event.GetToolArguments()
		item, err := f.complete(cursor, arguments.GetItemId(), FieldArguments, arguments.GetArgumentsJson())
		if err != nil {
			return err
		}
		f.recordEvidence(item.Cursor, entry)
	case "tool_output_delta":
		delta := event.GetToolOutputDelta()
		item, err := f.itemWrite(cursor, delta.GetItemId(), FieldOutput, delta.GetText(), true)
		if err != nil {
			return err
		}
		f.recordEvidence(item.Cursor, entry)
	case "item_completed":
		completed := event.GetItemCompleted()
		var item Item
		var err error
		switch completed.GetOutcome().(type) {
		case *eventpb.ItemCompleted_Text:
			if item, err = f.complete(cursor, completed.GetItemId(), FieldText, completed.GetText()); err != nil {
				return err
			}
			item.Completion = TextCompletion{}
		case *eventpb.ItemCompleted_Tool:
			tool := completed.GetTool()
			if item, err = f.complete(cursor, completed.GetItemId(), FieldOutput, tool.GetOutput()); err != nil {
				return err
			}
			item.Completion = ToolCompletion{Succeeded: tool.GetSucceeded()}
		default:
			return &ObservationNotUnderstoodError{cursor, "item_completed.outcome"}
		}
		f.recordEvidence(f.saveItem(item, cursor).Cursor, entry)
	case "conversation_reconciled":
		return f.reconcile(entry)
	case "harness_user_message_confirmed":
		confirmed := event.GetHarnessUserMessageConfirmed()
		ownerID := confirmed.GetHarnessMessageId()
		if ownerID == "" {
			ownerID = fmt.Sprintf("confirmed:%d", cursor)
		}
		epoch := f.state.Position.ProjectionEpoch
		text, err := f.write(PayloadOwner{epoch, cursor, ownerID, cursor}, nil, cursor, FieldConfirmedInput, confirmed.GetText(), false)
		if err != nil {
			return err
		}
		input := ConfirmedInput{
			ProjectionEpoch:  epoch,
			Cursor:           cursor,
			RevisionCursor:   cursor,
			HarnessMessageID: confirmed.GetHarnessMessageId(),
			OriginCommandIDs: append([]string{}, confirmed.GetOriginCommandIds()...),
			Text:             text,
		}
		if confirmed.GetTurnId() != "" {
			input.TurnID = proto.String(confirmed.GetTurnId())
		}
		f.confirmed.set(cursor, input)
		for _, commandID := range confirmed.GetOriginCommandIds() {
			if err := f.settle(entry, commandID, OutcomeEffected, nil); err != nil {
				return err
			}
		}
		f.recordEvidence(cursor, entry)
	case "command_admitted":
		if err := f.admit(cursor, event.GetCommandAdmitted().GetCommand()); err != nil {
			return err
		}
		f.recordEvidence(cursor, entry)
	case "command_failed":
		failed := event.GetCommandFailed()
		return f.settle(entry, failed.GetCommandId(), OutcomeFailed, proto.String(failed.GetReason()))
	case "command_noop":
		noop := event.GetCommandNoop()
		return f.settle(entry, noop.GetCommandId(), OutcomeNoop, proto.String(noop.GetReason()))
	case "execution_progress":
		progress := event.GetExecutionProgress()
		if progress.GetKind() == "session" && progress.GetPhase() == "blocked" {
			var payload struct {
				Reason *string `json:"reason"`
			}
			if err := json.Unmarshal([]byte(progress.GetPayloadJson()), &payload); err != nil || payload.Reason == nil {
				return &ContractError{Cursor: cursor, Message: "session blocked progress has no JSON reason"}
			}
			controls := f.state.Controls
			controls.BlockedReason = payload.Reason
			f.state.Controls = controls
		}
	default:
		switch {
		case lifecycleObservations[observation]:
			if err := f.recordLifecycle(entry, observation); err != nil {
				return err
			}
			f.recordEvidence(cursor, entry)
		case silentObservations[observation]:
		default:
			return &ObservationNotUnderstoodError{cursor, observation}
		}
	}
	return nil
}

func (f *folder) reconcile(entry *eventlogpb.EventEntry) error {
	cursor := entry.GetCursor()
	for _, recovered := range entry.GetEvent().GetConversationReconciled().GetItems() {
		disposition := recovered.GetDisposition()
		switch disposition {
		case eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED,
			eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT,
			eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN,
			eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED:
		default:
			return &ObservationNotUnderstoodError{cursor, "conversation_reconciled.disposition"}
		}
		item, err := f.item(cursor, recovered.GetItemId())
		if err != nil {
			return err
		}
		// A reconciliation that could not tell never overrides an earlier one that could; any other
		// disposition replaces it.
		if disposition == eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN && item.Recovery != nil &&
			*item.Recovery != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN {
			continue
		}
		revised := disposition == eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED
		if revised != (recovered.GetReplacement() != nil) {
			return &ContractError{cursor, "revised recovery requires replacement content"}
		}
		if revised {
			content := recovered.GetReplacement()
			if item.Kind == eventpb.ItemKind_ITEM_KIND_TOOL_CALL {
				if _, err := f.complete(cursor, item.ItemID, FieldArguments, content.GetArgumentsJson()); err != nil {
					return err
				}
				item, err = f.complete(cursor, item.ItemID, FieldOutput, content.GetOutput())
			} else {
				item, err = f.complete(cursor, item.ItemID, FieldText, content.GetText())
			}
			if err != nil {
				return err
			}
		}
		item.Recovery = &disposition
		item.RecoveryReason = nil
		if recovered.GetReason() != "" {
			item.RecoveryReason = proto.String(recovered.GetReason())
		}
		f.recordEvidence(f.saveItem(item, cursor).Cursor, entry)
	}
	return nil
}

// Advance folds an original-source prefix, contiguous but for settled ranges, without mutating the
// values its caller owns.
func Advance(state ViewState, batch EventBatch, prior PriorEntities) (ProjectionBatch, error) {
	position := state.Position
	if position.SourceID == "" || position.ProjectionEpoch == "" {
		return ProjectionBatch{}, errors.New("projection source and epoch are required")
	}
	if batch.SourceID != position.SourceID {
		return ProjectionBatch{}, errors.New("batch source does not match projection")
	}
	if batch.AfterCursor != position.ThroughCursor {
		return ProjectionBatch{}, errors.New("batch checkpoint does not match projection")
	}
	f := &folder{state: state, initialThrough: position.ThroughCursor, prior: prior}
	for _, supplied := range batch.Entries {
		entry := proto.Clone(supplied).(*eventlogpb.EventEntry)
		if err := f.advanceTo(entry, batch); err != nil {
			var notUnderstood *ObservationNotUnderstoodError
			if errors.As(err, &notUnderstood) {
				return ProjectionBatch{}, err
			}
			return ProjectionBatch{}, &ContractError{Cursor: entry.GetCursor(), Message: err.Error()}
		}
	}
	return ProjectionBatch{
		State:                 f.state,
		ItemUpserts:           f.items.all(),
		ConfirmedInputUpserts: f.confirmed.all(),
		LifecycleUpserts:      f.lifecycle.all(),
		CommandUpserts:        f.commands.all(),
		EvidenceUpserts:       f.evidence,
		PayloadWrites:         f.writes.all(),
	}, nil
}

func (f *folder) advanceTo(entry *eventlogpb.EventEntry, batch EventBatch) error {
	cursor := entry.GetCursor()
	if cursor != settledcursors.NextStored(f.state.Position.ThroughCursor, batch.Settled) {
		return fmt.Errorf("noncontiguous source cursor: %d", cursor)
	}
	if entry.GetOrigin().GetSourceId() != batch.SourceID || entry.GetOrigin().GetSequence() != cursor {
		return errors.New("entry does not belong to the original source archive")
	}
	if err := f.fold(entry); err != nil {
		return err
	}
	f.state.Position.ThroughCursor = cursor
	return nil
}
