package store

import (
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

func TestDescribeDispatchMapsStoredStates(t *testing.T) {
	tests := []struct {
		stored string
		want   dispatcherpb.DeliveryState
	}{
		{QueueState, dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER},
		{DeliveringState, dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER},
		{AdmittedState, dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED},
		{FailedState, dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED},
		{CancelledState, dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER},
		{UnknownState, dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		// Unspecified is a proto zero value, not a stored state; it must not leak through.
		{"UNSPECIFIED", dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		{"FUTURE_STATE", dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
	}
	for _, tt := range tests {
		dispatch, err := DescribeDispatch(&Dispatch{State: tt.stored, Operation: "submit_input"})
		if err != nil {
			t.Fatalf("DescribeDispatch(state=%q) error = %v", tt.stored, err)
		}
		if got := dispatch.GetState(); got != tt.want {
			t.Errorf("DescribeDispatch(state=%q) state = %s, want %s", tt.stored, got, tt.want)
		}
	}
}

func TestDescribeDispatchMapsStoredOperations(t *testing.T) {
	tests := map[string]commandpb.Operation{
		"submit_input":            commandpb.Operation_OPERATION_SUBMIT_INPUT,
		"change_model":            commandpb.Operation_OPERATION_CHANGE_MODEL,
		"change_reasoning_effort": commandpb.Operation_OPERATION_CHANGE_REASONING_EFFORT,
		"interrupt_turn":          commandpb.Operation_OPERATION_INTERRUPT_TURN,
	}
	for stored, want := range tests {
		dispatch, err := DescribeDispatch(&Dispatch{State: QueueState, Operation: stored})
		if err != nil {
			t.Fatalf("DescribeDispatch(operation=%q) error = %v", stored, err)
		}
		if got := dispatch.GetOperation(); got != want {
			t.Errorf("DescribeDispatch(operation=%q) operation = %s, want %s", stored, got, want)
		}
	}
	// COMPACT exists in the proto but has no stored dispatch mapping; it must stay an error.
	for _, stored := range []string{"compact", "SUBMIT_INPUT", ""} {
		if _, err := DescribeDispatch(&Dispatch{State: QueueState, Operation: stored}); err == nil {
			t.Errorf("DescribeDispatch(operation=%q) succeeded, want error", stored)
		}
	}
}
