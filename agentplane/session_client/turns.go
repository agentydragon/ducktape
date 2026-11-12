package sessionclient

import (
	"errors"
	"slices"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

// TurnInput records one retained harness input and the command IDs that contributed it. Dispatcher
// accepts opaque command IDs, including inputs submitted outside the UUID-based delegation tools.
type TurnInput struct {
	Text       string     `json:"text"`
	CommandIDs []OpaqueID `json:"command_ids"`
}

// TurnToolCall is the stable, compact part of an observed tool call.
type TurnToolCall struct {
	ItemID OpaqueID `json:"item_id"`
	Name   string   `json:"name"`
}

// TurnView contains observed conversation inputs and tool calls plus terminal status only when a
// matching TurnCompleted event exists. A turn ID remains opaque because providers own its shape.
type TurnView struct {
	TurnID                OpaqueID       `json:"turn_id"`
	Input                 []TurnInput    `json:"input"`
	ToolCalls             []TurnToolCall `json:"tool_calls"`
	Status                *TurnStatus    `json:"status,omitempty"`
	Error                 *string        `json:"error,omitempty"`
	FinalAssistantMessage *string        `json:"final_assistant_message,omitempty"`
}

type turnInputKey struct {
	message string
	cursor  uint64
}

// TurnProjection folds one chronological Session event stream. The zero value is ready to use.
// Transport, pagination, coverage gaps and authorization belong to its caller.
type TurnProjection struct {
	current   *TurnView
	itemKinds map[OpaqueID]eventpb.ItemKind
	inputs    map[turnInputKey]int
}

func cloneTurn(turn TurnView) TurnView {
	clone := turn
	clone.Input = slices.Clone(turn.Input)
	for i := range clone.Input {
		clone.Input[i].CommandIDs = slices.Clone(turn.Input[i].CommandIDs)
	}
	clone.ToolCalls = slices.Clone(turn.ToolCalls)
	if turn.Status != nil {
		value := *turn.Status
		clone.Status = &value
	}
	if turn.Error != nil {
		value := *turn.Error
		clone.Error = &value
	}
	if turn.FinalAssistantMessage != nil {
		value := *turn.FinalAssistantMessage
		clone.FinalAssistantMessage = &value
	}
	return clone
}

func (p *TurnProjection) startOfDifferentTurn(entry *eventlogpb.EventEntry) bool {
	event := entry.GetEvent()
	var turnID string
	switch observation := event.GetObservation().(type) {
	case *eventpb.Event_TurnStarted:
		turnID = observation.TurnStarted.GetTurnId()
	case *eventpb.Event_HarnessUserMessageConfirmed:
		turnID = observation.HarnessUserMessageConfirmed.GetTurnId()
	case *eventpb.Event_TurnCompleted:
		turnID = observation.TurnCompleted.GetTurnId()
	}
	return p.current != nil && turnID != "" && string(p.current.TurnID) != turnID
}

// Partial returns a detached snapshot of the unfinished turn, or nil between turns.
func (p *TurnProjection) Partial() *TurnView {
	if p.current == nil {
		return nil
	}
	turn := cloneTurn(*p.current)
	return &turn
}

// Add returns a completed turn only on its TurnCompleted event. A different turn before that
// boundary is incomplete History; callers may retain Partial but must report the read failure.
func (p *TurnProjection) Add(entry *eventlogpb.EventEntry) (*TurnView, error) {
	if p.startOfDifferentTurn(entry) {
		return nil, errors.New("new turn before the prior turn completed")
	}
	event := entry.GetEvent()
	if event == nil {
		return nil, errors.New("event missing")
	}
	switch observation := event.GetObservation().(type) {
	case *eventpb.Event_HarnessUserMessageConfirmed:
		confirmed := observation.HarnessUserMessageConfirmed
		if confirmed.GetTurnId() == "" {
			return nil, errors.New("input confirmation has no turn id")
		}
		current := p.ensure(confirmed.GetTurnId())
		for _, commandID := range confirmed.GetOriginCommandIds() {
			if commandID == "" {
				return nil, errors.New("input command id is empty")
			}
		}
		key := turnInputKey{message: confirmed.GetHarnessMessageId()}
		if key.message == "" {
			key.cursor = entry.GetCursor()
		}
		if p.inputs == nil {
			p.inputs = map[turnInputKey]int{}
		}
		index, replayed := p.inputs[key]
		if !replayed {
			index = len(current.Input)
			p.inputs[key] = index
			current.Input = append(current.Input, TurnInput{Text: confirmed.GetText(), CommandIDs: []OpaqueID{}})
		}
		input := &current.Input[index]
		for _, commandID := range confirmed.GetOriginCommandIds() {
			id := OpaqueID(commandID)
			if !slices.Contains(input.CommandIDs, id) {
				input.CommandIDs = append(input.CommandIDs, id)
			}
		}
	case *eventpb.Event_TurnStarted:
		turnID := observation.TurnStarted.GetTurnId()
		if turnID == "" {
			return nil, errors.New("turn start has no id")
		}
		p.ensure(turnID)
	case *eventpb.Event_ItemStarted:
		item := observation.ItemStarted
		if p.current != nil && item.GetItemId() != "" {
			if p.itemKinds == nil {
				p.itemKinds = map[OpaqueID]eventpb.ItemKind{}
			}
			p.itemKinds[OpaqueID(item.GetItemId())] = item.GetKind()
		}
		if item.GetKind() == eventpb.ItemKind_ITEM_KIND_TOOL_CALL {
			if p.current == nil || item.GetItemId() == "" {
				return nil, errors.New("tool call has no observed turn")
			}
			p.current.ToolCalls = append(p.current.ToolCalls, TurnToolCall{ItemID: OpaqueID(item.GetItemId()), Name: item.GetToolName()})
		}
	case *eventpb.Event_ItemCompleted:
		if text, ok := observation.ItemCompleted.GetOutcome().(*eventpb.ItemCompleted_Text); ok && p.itemKinds[OpaqueID(observation.ItemCompleted.GetItemId())] == eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT {
			if p.current != nil {
				p.current.FinalAssistantMessage = new(text.Text)
			}
		}
	case *eventpb.Event_TurnCompleted:
		completed := observation.TurnCompleted
		if completed.GetTurnId() == "" {
			return nil, errors.New("turn completion has no id")
		}
		current := p.ensure(completed.GetTurnId())
		statusValue, ok := turnStatus(completed.GetStatus())
		if !ok {
			return nil, errors.New("turn completion has an unknown status")
		}
		current.Status = &statusValue
		if completed.GetError() != "" {
			current.Error = new(completed.GetError())
		}
		p.current = nil
		p.itemKinds = nil
		p.inputs = nil
		return current, nil
	}
	return nil, nil
}

func (p *TurnProjection) ensure(turnID string) *TurnView {
	if p.current == nil {
		p.current = &TurnView{TurnID: OpaqueID(turnID), Input: []TurnInput{}, ToolCalls: []TurnToolCall{}}
		p.itemKinds = nil
	}
	return p.current
}

func turnStatus(status eventpb.TurnStatus) (TurnStatus, bool) {
	if _, ok := eventpb.TurnStatus_name[int32(status)]; !ok || status == eventpb.TurnStatus_TURN_STATUS_UNSPECIFIED {
		return "", false
	}
	return TurnStatus(status.String()), true
}
