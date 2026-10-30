package threadfold

import (
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const (
	testSource = "test-source"
	testEpoch  = "test-epoch"
)

func entry(cursor uint64, event *eventpb.Event, sourceSequences ...uint64) *eventlogpb.EventEntry {
	event.SourceSequences = sourceSequences
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: testSource, Sequence: cursor},
		Event:  event,
	}
}

func executionProgress(cursor uint64, kind, phase, payload string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{
		ExecutionProgress: &eventpb.ExecutionProgress{Kind: kind, Phase: phase, PayloadJson: payload},
	}})
}

func textDelta(cursor uint64, itemID, text string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TextDelta{
		TextDelta: &eventpb.TextDelta{ItemId: itemID, Text: text},
	}})
}

func argumentsDelta(cursor uint64, itemID, partial string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ToolArgumentsDelta{
		ToolArgumentsDelta: &eventpb.ToolArgumentsDelta{ItemId: itemID, PartialJson: partial},
	}})
}

func outputDelta(cursor uint64, itemID, text string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ToolOutputDelta{
		ToolOutputDelta: &eventpb.ToolOutputDelta{ItemId: itemID, Text: text},
	}})
}

func toolArguments(cursor uint64, itemID, arguments string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ToolArguments{
		ToolArguments: &eventpb.ToolArguments{ItemId: itemID, ArgumentsJson: arguments},
	}})
}

func itemStarted(cursor uint64, itemID string, kind eventpb.ItemKind, toolName string, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: itemID, Kind: kind, ToolName: toolName},
	}}, sourceSequences...)
}

func textCompleted(cursor uint64, itemID, text string, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{
		ItemCompleted: &eventpb.ItemCompleted{ItemId: itemID, Outcome: &eventpb.ItemCompleted_Text{Text: text}},
	}}, sourceSequences...)
}

func toolCompleted(cursor uint64, itemID, output string, succeeded bool, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{
		ItemCompleted: &eventpb.ItemCompleted{ItemId: itemID, Outcome: &eventpb.ItemCompleted_Tool{
			Tool: &eventpb.ToolResult{Output: output, Succeeded: succeeded},
		}},
	}}, sourceSequences...)
}

func admitted(cursor uint64, command *commandpb.Command) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
		CommandAdmitted: &eventpb.CommandAdmitted{Command: command},
	}})
}

func submitInput(cursor uint64, commandID, text string) *eventlogpb.EventEntry {
	return admitted(cursor, &commandpb.Command{
		CommandId: commandID,
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}},
	})
}

func changeModel(cursor uint64, commandID, model string) *eventlogpb.EventEntry {
	return admitted(cursor, &commandpb.Command{
		CommandId: commandID,
		Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: model}},
	})
}

func reconciled(cursor uint64, turnID string, items ...*eventpb.ItemRecovery) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ConversationReconciled{
		ConversationReconciled: &eventpb.ConversationReconciled{TurnId: turnID, Items: items},
	}})
}

// memory stands in for the storage worker: it preloads the rows a batch touches and keeps what the fold
// returns.
type memory struct {
	state    ViewState
	items    map[string]Item
	commands map[string]CommandSummary
	payloads map[PayloadRef]string
	evidence []EvidenceAssociation
}

func newMemory(t *testing.T) *memory {
	t.Helper()
	state, err := Initial(testSource, testEpoch)
	if err != nil {
		t.Fatal(err)
	}
	return &memory{
		state:    state,
		items:    map[string]Item{},
		commands: map[string]CommandSummary{},
		payloads: map[PayloadRef]string{},
	}
}

func (m *memory) prior(batch EventBatch) PriorEntities {
	keys := Touched(batch)
	prior := PriorEntities{
		Items:    map[string]*Item{},
		Commands: map[string]*CommandSummary{},
		Bodies:   map[PayloadRef]string{},
	}
	for _, id := range keys.ItemIDs {
		prior.Items[id] = nil
		if item, ok := m.items[id]; ok {
			prior.Items[id] = &item
		}
	}
	for _, id := range keys.CommandIDs {
		prior.Commands[id] = nil
		if command, ok := m.commands[id]; ok {
			prior.Commands[id] = &command
		}
	}
	for _, completed := range keys.Completed {
		if item := prior.Items[completed.ItemID]; item != nil {
			if reference, _ := item.Payload(completed.Field); reference != nil {
				prior.Bodies[*reference] = m.payloads[*reference]
			}
		}
	}
	return prior
}

func (m *memory) tryApply(entries ...*eventlogpb.EventEntry) (ProjectionBatch, error) {
	batch := EventBatch{SourceID: testSource, AfterCursor: m.state.Position.ThroughCursor, Entries: entries}
	result, err := Advance(m.state, batch, m.prior(batch))
	if err != nil {
		return ProjectionBatch{}, err
	}
	for _, write := range result.PayloadWrites {
		text := write.Fragment()
		if appended, ok := write.(AppendPayload); ok && appended.Base != nil {
			text = m.payloads[*appended.Base] + text
		}
		m.payloads[write.Ref()] = text
	}
	for _, item := range result.ItemUpserts {
		m.items[item.ItemID] = item
	}
	for _, command := range result.CommandUpserts {
		m.commands[command.CommandID] = command
	}
	m.evidence = append(m.evidence, result.EvidenceUpserts...)
	m.state = result.State
	return result, nil
}

func (m *memory) apply(t *testing.T, entries ...*eventlogpb.EventEntry) ProjectionBatch {
	t.Helper()
	result, err := m.tryApply(entries...)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

// script is a conversation exercising coalesced inputs, interleaved items, authoritative completions and
// command settlement.
func script() []*eventlogpb.EventEntry {
	return []*eventlogpb.EventEntry{
		submitInput(1, "input-1", "first"),
		submitInput(2, "input-2", "second"),
		entry(3, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
			HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
				HarnessMessageId: "message-1", Text: "first\nsecond",
				OriginCommandIds: []string{"input-1", "input-2"}, TurnId: "turn-1",
			},
		}}, 11, 12),
		entry(4, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{
			TurnStarted: &eventpb.TurnStarted{TurnId: "turn-1", Model: "model-a"},
		}}),
		withSequences(textDelta(5, "answer", "hel"), 13),
		itemStarted(6, "tool-a", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, "read", 14),
		withSequences(argumentsDelta(7, "tool-a", `{"p"`), 15),
		itemStarted(8, "tool-b", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, "write", 16),
		toolCompleted(9, "tool-b", "B", true, 17),
		withSequences(toolArguments(10, "tool-a", `{"path":"x"}`), 18),
		withSequences(outputDelta(11, "tool-a", "part"), 19),
		toolCompleted(12, "tool-a", "", false, 20),
		itemStarted(13, "answer", eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, "", 21),
		textCompleted(14, "answer", "hello", 22),
		changeModel(15, "model-1", "model-b"),
		entry(16, &eventpb.Event{Observation: &eventpb.Event_ModelChanged{
			ModelChanged: &eventpb.ModelChanged{CommandId: "model-1", Model: "model-b"},
		}}),
		entry(17, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
			TurnCompleted: &eventpb.TurnCompleted{TurnId: "turn-1", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED},
		}}),
	}
}

func withSequences(e *eventlogpb.EventEntry, sequences ...uint64) *eventlogpb.EventEntry {
	e.Event.SourceSequences = sequences
	return e
}

func replay(t *testing.T, entries []*eventlogpb.EventEntry, sizes ...int) *memory {
	t.Helper()
	store := newMemory(t)
	start := 0
	for _, size := range sizes {
		store.apply(t, entries[start:start+size]...)
		start += size
	}
	if start != len(entries) {
		t.Fatalf("batches cover %d of %d entries", start, len(entries))
	}
	return store
}

func TestEffortChangeSettlesItsCommandAndUpdatesControls(t *testing.T) {
	store := newMemory(t)
	result := store.apply(
		t,
		admitted(1, &commandpb.Command{
			CommandId: "effort-1",
			Operation: &commandpb.Command_ChangeReasoningEffort{
				ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"},
			},
		}),
		entry(2, &eventpb.Event{Observation: &eventpb.Event_ReasoningEffortChanged{
			ReasoningEffortChanged: &eventpb.ReasoningEffortChanged{CommandId: "effort-1", PreviousEffort: "low", Effort: "high"},
		}}),
	)
	command := store.commands["effort-1"]
	if got := store.state.Controls.AppliedReasoningEffort; got == nil || *got != "high" {
		t.Errorf("applied reasoning effort = %v, want high", got)
	}
	if command.Outcome != OutcomeEffected || command.RequestedValue == nil || *command.RequestedValue != "high" {
		t.Errorf("command = %+v, want effected with requested value high", command)
	}
	if got := result.LifecycleUpserts[0].Observation; got != "reasoning_effort_changed" {
		t.Errorf("first lifecycle segment = %q", got)
	}
}

func TestSessionBlockedProgressIsSilentAndSurvivesCheckpointPaging(t *testing.T) {
	store := newMemory(t)
	blocked := store.apply(t, executionProgress(1, "session", "blocked", `{"reason":"provider unavailable"}`))
	if got := store.state.Controls.BlockedReason; got == nil || *got != "provider unavailable" {
		t.Fatalf("blocked reason = %v, want provider unavailable", got)
	}
	if len(blocked.LifecycleUpserts) != 0 {
		t.Fatalf("blocked progress created lifecycle rows: %+v", blocked.LifecycleUpserts)
	}

	rows, err := OrderedEntityRows(uuid.New(), blocked, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].EntityKind != KindViewState {
		t.Fatalf("blocked progress rows = %+v, want only view state", rows)
	}
	var encoded struct {
		Controls map[string]json.RawMessage `json:"controls"`
	}
	if err := json.Unmarshal(rows[0].State, &encoded); err != nil {
		t.Fatal(err)
	}
	if got := string(encoded.Controls["blocked_reason"]); got != `"provider unavailable"` {
		t.Fatalf("serialized blocked reason = %s", got)
	}
	stored, err := ReadViewState(rows[0].State)
	if err != nil {
		t.Fatal(err)
	}

	// Restore the state from the stored checkpoint before folding the next page.
	restored := ViewState{
		Position: store.state.Position,
		Controls: Controls{
			AppliedModel: stored.Controls.AppliedModel, AppliedReasoningEffort: stored.Controls.AppliedReasoningEffort,
			ActiveTurnID: stored.Controls.ActiveTurnID, HarnessState: stored.Controls.HarnessState,
			BlockedReason: stored.Controls.BlockedReason,
		},
	}
	continued, err := Advance(restored, EventBatch{
		SourceID: testSource, AfterCursor: 1,
		Entries: []*eventlogpb.EventEntry{
			executionProgress(2, "session", "failed", `{"reason":"ordinary failure"}`),
			executionProgress(3, "provider", "blocked", `{"reason":"nested operation blocked"}`),
			entry(4, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
				TurnCompleted: &eventpb.TurnCompleted{TurnId: "failed-turn", Status: eventpb.TurnStatus_TURN_STATUS_FAILED},
			}}),
			entry(5, &eventpb.Event{Observation: &eventpb.Event_HarnessExited{HarnessExited: &eventpb.HarnessExited{}}}),
		},
	}, PriorEntities{Items: map[string]*Item{}, Commands: map[string]*CommandSummary{}, Bodies: map[PayloadRef]string{}})
	if err != nil {
		t.Fatal(err)
	}
	if got := continued.State.Controls.BlockedReason; got == nil || *got != "provider unavailable" {
		t.Fatalf("blocked reason after restore and ordinary progress = %v", got)
	}
	if got := []string{continued.LifecycleUpserts[0].Observation, continued.LifecycleUpserts[1].Observation}; !reflect.DeepEqual(got, []string{"turn_completed", "harness_exited"}) {
		t.Fatalf("lifecycle rows = %v, want only turn completion and harness exit", got)
	}

	ordinary := newMemory(t)
	ordinary.apply(
		t,
		executionProgress(1, "session", "failed", `{"reason":"turn failed"}`),
		executionProgress(2, "provider", "blocked", `{"reason":"operation blocked"}`),
	)
	if ordinary.state.Controls.BlockedReason != nil {
		t.Fatalf("ordinary progress set blocked reason %v", ordinary.state.Controls.BlockedReason)
	}
}

func TestBatchPartitionsHaveIdenticalMaterialization(t *testing.T) {
	entries := script()
	whole := replay(t, entries, len(entries))
	for split := 1; split < len(entries); split++ {
		if got := replay(t, entries, split, len(entries)-split); !reflect.DeepEqual(got, whole) {
			t.Fatalf("split at %d materializes differently:\n got %+v\nwant %+v", split, got, whole)
		}
	}
}

func TestParallelOldItemUpdatesKeepPositionsFieldsAndEvidence(t *testing.T) {
	store := replay(t, script(), 17)
	answer, first, second := store.items["answer"], store.items["tool-a"], store.items["tool-b"]
	if got := []uint64{answer.Cursor, first.Cursor, second.Cursor}; !reflect.DeepEqual(got, []uint64{5, 6, 8}) {
		t.Errorf("item cursors = %v, want [5 6 8]", got)
	}
	if got := []uint64{first.RevisionCursor, second.RevisionCursor}; !reflect.DeepEqual(got, []uint64{12, 9}) {
		t.Errorf("tool revision cursors = %v, want [12 9]", got)
	}
	for _, item := range []Item{answer, first, second} {
		if item.TurnID == nil || *item.TurnID != "turn-1" {
			t.Errorf("item %s turn = %v, want turn-1", item.ItemID, item.TurnID)
		}
	}
	if first.Arguments == nil || first.Output == nil {
		t.Fatalf("tool-a lacks a payload: %+v", first)
	}
	if got := store.payloads[*first.Arguments]; got != `{"path":"x"}` {
		t.Errorf("arguments body = %q", got)
	}
	if got := store.payloads[*first.Output]; got != "" {
		t.Errorf("output body = %q, want the authoritative empty output", got)
	}
	if first.Output.Field != FieldOutput || first.Arguments.Field != FieldArguments {
		t.Errorf("payload fields = %s, %s", first.Output.Field, first.Arguments.Field)
	}
	if first.Completion != (ToolCompletion{Succeeded: false}) || answer.Completion != (TextCompletion{}) {
		t.Errorf("completions = %v, %v", first.Completion, answer.Completion)
	}
	var observed []uint64
	for _, association := range store.evidence {
		if association.EntityCursor == first.Cursor {
			observed = append(observed, association.ObservationCursor)
		}
	}
	if want := []uint64{6, 7, 10, 11, 12}; !reflect.DeepEqual(observed, want) {
		t.Errorf("evidence of tool-a = %v, want %v", observed, want)
	}
}

func streaming(cursor uint64, field PayloadField, text string) *eventlogpb.EventEntry {
	switch field {
	case FieldText:
		return textDelta(cursor, "item", text)
	case FieldArguments:
		return argumentsDelta(cursor, "item", text)
	}
	return outputDelta(cursor, "item", text)
}

func authoritative(cursor uint64, field PayloadField, text string) *eventlogpb.EventEntry {
	switch field {
	case FieldText:
		return textCompleted(cursor, "item", text)
	case FieldArguments:
		return toolArguments(cursor, "item", text)
	}
	return toolCompleted(cursor, "item", text, true)
}

// partitions calls visit with every way to cut entries into consecutive batches.
func partitions(entries []*eventlogpb.EventEntry, visit func([][]*eventlogpb.EventEntry)) {
	for cuts := 0; cuts < 1<<(len(entries)-1); cuts++ {
		var batches [][]*eventlogpb.EventEntry
		start := 0
		for end := 1; end <= len(entries); end++ {
			if end == len(entries) || cuts&(1<<(end-1)) != 0 {
				batches = append(batches, entries[start:end])
				start = end
			}
		}
		visit(batches)
	}
}

func TestAnAuthoritativeValueKeepsTheBodyOnlyWhenItIsWhatStreamed(t *testing.T) {
	for _, field := range []PayloadField{FieldText, FieldArguments, FieldOutput} {
		for _, test := range []struct {
			name   string
			deltas []string
			final  string
			kept   bool
		}{
			{"as-streamed", []string{"hel", "lo"}, "hello", true},
			{"rewritten", []string{"hel", "lo"}, "Hello", false},
			{"not-streamed", nil, "hello", false},
		} {
			t.Run(string(field)+"/"+test.name, func(t *testing.T) {
				var entries []*eventlogpb.EventEntry
				for i, text := range test.deltas {
					entries = append(entries, streaming(uint64(i+1), field, text))
				}
				cursor := uint64(len(entries) + 1)
				entries = append(entries, authoritative(cursor, field, test.final))
				generation := cursor
				if test.kept {
					generation = 1
				}
				reference := PayloadRef{testEpoch, 1, "item", field, cursor, generation}
				// Kept: the streamed revision's generation and chunks, and no new text to store.
				var lastWrite PayloadWrite = ReplacePayload{reference, test.final}
				if test.kept {
					base := reference
					base.RevisionCursor = cursor - 1
					lastWrite = AppendPayload{&base, reference, ""}
				}
				whole := newMemory(t)
				whole.apply(t, entries...)
				if got, _ := whole.items["item"].Payload(field); got == nil || *got != reference {
					t.Fatalf("item payload = %v, want %v", got, reference)
				}
				if got := whole.payloads[reference]; got != test.final {
					t.Fatalf("body = %q, want %q", got, test.final)
				}
				partitions(entries, func(batches [][]*eventlogpb.EventEntry) {
					store := newMemory(t)
					var last ProjectionBatch
					for _, batch := range batches {
						last = store.apply(t, batch...)
					}
					if !reflect.DeepEqual(store, whole) {
						t.Fatalf("batches of %d entries materialize differently", len(batches))
					}
					if got := last.PayloadWrites[len(last.PayloadWrites)-1]; !reflect.DeepEqual(got, lastWrite) {
						t.Fatalf("last write = %+v, want %+v", got, lastWrite)
					}
				})
			})
		}
	}
}

func TestCommandsSettleCoalescedInputAndObservedModelEffect(t *testing.T) {
	store := replay(t, script(), 17)
	for _, id := range []string{"input-1", "input-2", "model-1"} {
		if got := store.commands[id].Outcome; got != OutcomeEffected {
			t.Errorf("command %s outcome = %s, want effected", id, got)
		}
	}
	if got := store.commands["model-1"].RequestedValue; got == nil || *got != "model-b" {
		t.Errorf("model-1 requested value = %v", got)
	}
	if got := store.state.Controls.AppliedModel; got == nil || *got != "model-b" {
		t.Errorf("applied model = %v", got)
	}
	if store.state.Controls.ActiveTurnID != nil {
		t.Errorf("active turn = %q, want none after the turn completed", *store.state.Controls.ActiveTurnID)
	}
	type pair struct{ entity, observation uint64 }
	var got []pair
	for _, association := range store.evidence {
		if association.EntityCursor == 1 || association.EntityCursor == 2 || association.EntityCursor == 15 {
			got = append(got, pair{association.EntityCursor, association.ObservationCursor})
		}
	}
	if want := []pair{{1, 1}, {2, 2}, {1, 3}, {2, 3}, {15, 15}, {15, 16}}; !reflect.DeepEqual(got, want) {
		t.Errorf("command evidence = %v, want %v", got, want)
	}
}

func contractError(t *testing.T, err error) {
	t.Helper()
	var contract *ContractError
	if !errors.As(err, &contract) {
		t.Fatalf("error = %v, want a ContractError", err)
	}
}

func TestMissingLookupIsNotAbsenceAndPreloadedRowsCannotBeFromThisBatch(t *testing.T) {
	observed := textDelta(1, "item", "x")
	initial, err := Initial(testSource, testEpoch)
	if err != nil {
		t.Fatal(err)
	}
	batch := EventBatch{SourceID: testSource, Entries: []*eventlogpb.EventEntry{observed}}
	_, err = Advance(initial, batch, PriorEntities{})
	contractError(t, err)
	future := &Item{ProjectionEpoch: testEpoch, ItemID: "item", Cursor: 1, RevisionCursor: 2}
	_, err = Advance(initial, batch, PriorEntities{Items: map[string]*Item{"item": future}})
	contractError(t, err)

	store := newMemory(t)
	store.apply(t, observed)
	item := store.items["item"]
	_, err = Advance(store.state, EventBatch{
		SourceID: testSource, AfterCursor: 1, Entries: []*eventlogpb.EventEntry{textCompleted(2, "item", "x")},
	}, PriorEntities{Items: map[string]*Item{"item": &item}})
	contractError(t, err)
}

func TestRejectsWrongFieldRefAndUnknownKindWithoutMutatingInputs(t *testing.T) {
	store := newMemory(t)
	store.apply(t, textDelta(1, "item", "x"))
	prior := store.items["item"]
	invalid := Item{
		ProjectionEpoch: testEpoch, ItemID: "item", Cursor: prior.Cursor, RevisionCursor: prior.RevisionCursor,
		Text: &PayloadRef{testEpoch, 1, "item", FieldOutput, 1, 1},
	}
	_, err := Advance(store.state, EventBatch{
		SourceID: testSource, AfterCursor: 1, Entries: []*eventlogpb.EventEntry{textDelta(2, "item", "y")},
	}, PriorEntities{Items: map[string]*Item{"item": &invalid}})
	contractError(t, err)

	unknown := itemStarted(2, "other", eventpb.ItemKind(99), "")
	serialized, err := proto.Marshal(unknown)
	if err != nil {
		t.Fatal(err)
	}
	state := store.state
	_, err = Advance(state, EventBatch{
		SourceID: testSource, AfterCursor: 1, Entries: []*eventlogpb.EventEntry{unknown},
	}, PriorEntities{Items: map[string]*Item{"other": nil}})
	var notUnderstood *ObservationNotUnderstoodError
	if !errors.As(err, &notUnderstood) || notUnderstood.Cursor != 2 {
		t.Fatalf("error = %v, want an ObservationNotUnderstoodError at cursor 2", err)
	}
	if after, _ := proto.Marshal(unknown); !reflect.DeepEqual(after, serialized) {
		t.Error("the unknown entry was mutated")
	}
	if !reflect.DeepEqual(store.state, state) {
		t.Error("the caller's state was mutated")
	}
}

func TestReconciliationPreservesObservedToolSuccessAndPayload(t *testing.T) {
	for _, disposition := range []eventpb.RecoveryDisposition{
		eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED,
		eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT,
		eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN,
	} {
		t.Run(disposition.String(), func(t *testing.T) {
			store := newMemory(t)
			store.apply(
				t,
				itemStarted(1, "tool", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ""),
				toolCompleted(2, "tool", "side effect committed", true),
			)
			original := store.items["tool"]
			result := store.apply(t, reconciled(3, "interrupted",
				&eventpb.ItemRecovery{ItemId: "tool", Disposition: disposition}))
			recovered := store.items["tool"]
			if recovered.Completion != (ToolCompletion{Succeeded: true}) {
				t.Errorf("completion = %v, want the observed success", recovered.Completion)
			}
			if !reflect.DeepEqual(recovered.Output, original.Output) {
				t.Errorf("output = %v, want %v", recovered.Output, original.Output)
			}
			if recovered.Recovery == nil || *recovered.Recovery != disposition {
				t.Errorf("recovery = %v, want %v", recovered.Recovery, disposition)
			}
			if len(result.PayloadWrites) != 0 {
				t.Errorf("payload writes = %v, want none", result.PayloadWrites)
			}
		})
	}
}

func revisedRecovery(itemID, reason string) *eventpb.ItemRecovery {
	return &eventpb.ItemRecovery{
		ItemId:      itemID,
		Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED,
		Reason:      reason,
		Replacement: &eventpb.RecoveredContent{ArgumentsJson: `{"command":"run"}`, Output: "interrupted"},
	}
}

func TestRecoveredToolContentDoesNotFabricateAnExecutionResult(t *testing.T) {
	store := newMemory(t)
	store.apply(
		t,
		itemStarted(1, "tool", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ""),
		outputDelta(2, "tool", "partial stdout"),
	)
	original := store.items["tool"].Output
	store.apply(t, reconciled(3, "lost", revisedRecovery("tool", "")))
	recovered := store.items["tool"]
	if recovered.Completion != nil {
		t.Errorf("completion = %v, want none", recovered.Completion)
	}
	if recovered.Output == nil || original == nil {
		t.Fatalf("outputs = %v, %v", recovered.Output, original)
	}
	if got := store.payloads[*recovered.Output]; got != "interrupted" {
		t.Errorf("recovered output = %q", got)
	}
	if got := store.payloads[*original]; got != "partial stdout" {
		t.Errorf("the observed output was overwritten with %q", got)
	}
	if recovered.Recovery == nil || *recovered.Recovery != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED {
		t.Errorf("recovery = %v", recovered.Recovery)
	}
}

func TestARecoveryNamingAnItemTwiceKeepsTheFirstWrites(t *testing.T) {
	store := newMemory(t)
	store.apply(t, itemStarted(1, "tool", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ""))
	result := store.apply(t, reconciled(2, "lost", revisedRecovery("tool", "first"), revisedRecovery("tool", "second")))
	if len(result.PayloadWrites) != 2 {
		t.Fatalf("payload writes = %+v, want the arguments and the output once each", result.PayloadWrites)
	}
	for _, write := range result.PayloadWrites {
		if _, replaced := write.(ReplacePayload); !replaced {
			t.Errorf("write %+v is not a replacement", write)
		}
	}
	if got := store.items["tool"].RecoveryReason; got == nil || *got != "second" {
		t.Errorf("recovery reason = %v, want the later one", got)
	}
}

func TestALaterReconciliationReplacesADecisionUnlessItCouldNotTell(t *testing.T) {
	const (
		retained = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED
		absent   = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT
		unknown  = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNKNOWN
		revised  = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED
	)
	for _, test := range []struct {
		first, second eventpb.RecoveryDisposition
		secondWins    bool
	}{
		{retained, unknown, false},
		{absent, unknown, false},
		{revised, unknown, false},
		{unknown, retained, true},
		{unknown, unknown, true},
		{retained, absent, true},
		{absent, revised, true},
	} {
		t.Run(test.first.String()+" then "+test.second.String(), func(t *testing.T) {
			store := newMemory(t)
			store.apply(t, itemStarted(1, "tool", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ""))
			for i, step := range []struct {
				disposition eventpb.RecoveryDisposition
				reason      string
			}{{test.first, "first"}, {test.second, "second"}} {
				recovery := &eventpb.ItemRecovery{ItemId: "tool", Disposition: step.disposition, Reason: step.reason}
				if step.disposition == revised {
					recovery.Replacement = &eventpb.RecoveredContent{ArgumentsJson: `{"command":"run"}`, Output: "interrupted"}
				}
				store.apply(t, reconciled(uint64(i+2), "turn", recovery))
			}
			wantDisposition, wantReason := test.first, "first"
			if test.secondWins {
				wantDisposition, wantReason = test.second, "second"
			}
			shown := store.items["tool"]
			if shown.Recovery == nil || *shown.Recovery != wantDisposition || shown.RecoveryReason == nil || *shown.RecoveryReason != wantReason {
				t.Errorf("recovery = %v %v, want %v %q", shown.Recovery, shown.RecoveryReason, wantDisposition, wantReason)
			}
		})
	}
}

func TestARevisedRecoveryWithoutReplacementIsAContractError(t *testing.T) {
	store := newMemory(t)
	store.apply(t, itemStarted(1, "tool", eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ""))
	_, err := store.tryApply(reconciled(2, "lost", &eventpb.ItemRecovery{
		ItemId: "tool", Disposition: eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED,
	}))
	contractError(t, err)
}

func TestSetupOutputAndFailureRemainVisibleLifecycleEvidence(t *testing.T) {
	store := newMemory(t)
	result := store.apply(
		t,
		entry(1, &eventpb.Event{Observation: &eventpb.Event_SetupStarted{SetupStarted: &eventpb.SetupStarted{}}}),
		entry(2, &eventpb.Event{Observation: &eventpb.Event_SetupOutput{SetupOutput: &eventpb.SetupOutput{
			Stream: &eventpb.SetupOutput_Stdout{Stdout: []byte("setup output\n")},
		}}}),
		entry(3, &eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{ExitCode: 7}}}),
	)
	var observations []string
	for _, segment := range result.LifecycleUpserts {
		observations = append(observations, segment.Observation)
	}
	if want := []string{"setup_started", "setup_output", "setup_finished"}; !reflect.DeepEqual(observations, want) {
		t.Errorf("lifecycle observations = %v, want %v", observations, want)
	}
	if got := string(result.LifecycleUpserts[1].Event.GetSetupOutput().GetStdout()); got != "setup output\n" {
		t.Errorf("setup output = %q", got)
	}
	if store.state.Position.ThroughCursor != 3 {
		t.Errorf("through cursor = %d, want 3", store.state.Position.ThroughCursor)
	}
}

func TestAReopenedJournalAdvancesTheProjectionWithoutARow(t *testing.T) {
	store := newMemory(t)
	result := store.apply(t, entry(1, &eventpb.Event{Observation: &eventpb.Event_JournalReopened{
		JournalReopened: &eventpb.JournalReopened{IncarnationUid: "test-incarnation"},
	}}))
	if len(result.LifecycleUpserts) != 0 || len(result.ItemUpserts) != 0 {
		t.Errorf("rows = %+v, want none", result)
	}
	if store.state.Position.ThroughCursor != 1 {
		t.Errorf("through cursor = %d, want 1", store.state.Position.ThroughCursor)
	}
}

func TestTouchedKeysAreSortedAndDropEmptyIdentities(t *testing.T) {
	keys := Touched(EventBatch{Entries: []*eventlogpb.EventEntry{
		textCompleted(1, "b", "x"),
		toolArguments(2, "a", "{}"),
		textDelta(3, "", "orphan"),
		entry(4, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "t"}}}),
		changeModel(5, "m", "x"),
	}})
	if want := []string{"a", "b"}; !reflect.DeepEqual(keys.ItemIDs, want) {
		t.Errorf("item ids = %v, want %v", keys.ItemIDs, want)
	}
	if want := []string{"m"}; !reflect.DeepEqual(keys.CommandIDs, want) {
		t.Errorf("command ids = %v, want %v", keys.CommandIDs, want)
	}
	if want := []ItemField{{"a", FieldArguments}, {"b", FieldText}}; !reflect.DeepEqual(keys.Completed, want) {
		t.Errorf("completed = %v, want %v", keys.Completed, want)
	}
}

func TestSettledRangesAreSkippedWhenCheckingContiguity(t *testing.T) {
	store := newMemory(t)
	batch := EventBatch{
		SourceID: testSource,
		Entries:  []*eventlogpb.EventEntry{textDelta(1, "item", "a"), textDelta(5, "item", "b")},
		Settled:  []*historypb.CursorRange{{First: 2, Last: 4}},
	}
	if _, err := Advance(store.state, batch, PriorEntities{Items: map[string]*Item{"item": nil}}); err != nil {
		t.Fatal(err)
	}
	batch.Settled = nil
	_, err := Advance(store.state, batch, PriorEntities{Items: map[string]*Item{"item": nil}})
	contractError(t, err)
}

func TestSamplerBoundariesPreserveDisplayProjection(t *testing.T) {
	entries := []*eventlogpb.EventEntry{
		entry(1, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{Boundary: "provider", Direction: "response", Payload: []byte(`{"type":"response.output_item.added"}`)}}}),
		entry(2, &eventpb.Event{Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{OperationId: "request", Kind: "provider", Phase: "response"}}}, 1),
		itemStarted(3, "answer", eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, "", 1),
		textCompleted(4, "answer", "central answer", 1),
	}
	state := replay(t, entries, len(entries))
	if len(state.items) != 1 || state.items["answer"].Completion == nil {
		t.Fatalf("missing normalized answer: %v", state.items)
	}
	found := false
	for _, evidence := range state.evidence {
		if len(evidence.SourceSequences) == 1 && evidence.SourceSequences[0] == 1 {
			found = true
		}
	}
	if !found {
		t.Fatal("boundary source lost from display evidence")
	}
}
