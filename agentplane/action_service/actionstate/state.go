// Package actionstate classifies Action lifecycle states and owns their stable storage names.
package actionstate

import "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"

// SQL rows and Notification payloads use this stable text vocabulary.
var storedStates = [...]struct {
	state action_servicepb.ActionState
	text  string
}{
	{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, "decision_pending"},
	{action_servicepb.ActionState_ACTION_STATE_ALLOWED, "allowed"},
	{action_servicepb.ActionState_ACTION_STATE_DENIED, "denied"},
	{action_servicepb.ActionState_ACTION_STATE_DISPATCHING, "dispatching"},
	{action_servicepb.ActionState_ACTION_STATE_RUNNING, "running"},
	{action_servicepb.ActionState_ACTION_STATE_SUCCEEDED, "succeeded"},
	{action_servicepb.ActionState_ACTION_STATE_FAILED, "failed"},
	{action_servicepb.ActionState_ACTION_STATE_CANCELLED, "cancelled"},
	{action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN, "execution_unknown"},
}

// FromStored decodes a stored Action lifecycle state.
func FromStored(text string) (action_servicepb.ActionState, bool) {
	for _, known := range storedStates {
		if text == known.text {
			return known.state, true
		}
	}
	return action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED, false
}

// Stored encodes an Action lifecycle state for persistent storage.
func Stored(state action_servicepb.ActionState) (string, bool) {
	for _, known := range storedStates {
		if state == known.state {
			return known.text, true
		}
	}
	return "", false
}

// StoredNames returns the text vocabulary in storage enum order.
func StoredNames() []string {
	names := make([]string, len(storedStates))
	for i, known := range storedStates {
		names[i] = known.text
	}
	return names
}

// Valid reports whether state is a supported lifecycle state.
func Valid(state action_servicepb.ActionState) bool {
	_, known := Stored(state)
	return known
}

// Decided reports whether state is a recorded state after decision_pending.
func Decided(state action_servicepb.ActionState) bool {
	return state != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING && Valid(state)
}

// Terminal reports whether the request cannot advance to another lifecycle state.
func Terminal(state action_servicepb.ActionState) bool {
	switch state {
	case action_servicepb.ActionState_ACTION_STATE_DENIED,
		action_servicepb.ActionState_ACTION_STATE_SUCCEEDED,
		action_servicepb.ActionState_ACTION_STATE_FAILED,
		action_servicepb.ActionState_ACTION_STATE_CANCELLED,
		action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		return true
	default:
		return false
	}
}
