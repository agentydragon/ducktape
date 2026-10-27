package action

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

func TestStatesRoundTripTheirActionServiceNumbers(t *testing.T) {
	states := []struct {
		state State
		proto action_servicepb.ActionState
	}{
		{StateDecisionPending, action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING},
		{StateAllowed, action_servicepb.ActionState_ACTION_STATE_ALLOWED},
		{StateDenied, action_servicepb.ActionState_ACTION_STATE_DENIED},
		{StateDispatching, action_servicepb.ActionState_ACTION_STATE_DISPATCHING},
		{StateRunning, action_servicepb.ActionState_ACTION_STATE_RUNNING},
		{StateSucceeded, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED},
		{StateFailed, action_servicepb.ActionState_ACTION_STATE_FAILED},
		{StateCancelled, action_servicepb.ActionState_ACTION_STATE_CANCELLED},
		{StateExecutionUnknown, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN},
	}
	for _, test := range states {
		if got, ok := test.state.Proto(); !ok || got != test.proto {
			t.Errorf("%q.Proto() = %v, %v; want %v, true", test.state, got, ok, test.proto)
		}
		if got, ok := StateFromProto(test.proto); !ok || got != test.state {
			t.Errorf("StateFromProto(%v) = %q, %v; want %q, true", test.proto, got, ok, test.state)
		}
	}
	for _, proto := range []action_servicepb.ActionState{
		action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED,
		99,
	} {
		if got, ok := StateFromProto(proto); ok {
			t.Errorf("StateFromProto(%v) = %q, true; want unknown", proto, got)
		}
	}
	if _, ok := State("future_state").Proto(); ok {
		t.Error("unknown stored state has a protobuf form")
	}
}

func TestActorProtoPreservesRecordedIdentities(t *testing.T) {
	actors := []Actor{
		CallerActor{Account: ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}},
		OperatorActor{Issuer: "https://issuer.example", Subject: "subject-123"},
	}
	for _, actor := range actors {
		principal, err := ActorToProto(actor)
		if err != nil {
			t.Fatalf("ActorToProto(%#v): %v", actor, err)
		}
		got, err := ActorFromProto(principal)
		if err != nil || got != actor {
			t.Errorf("ActorFromProto(ActorToProto(%#v)) = %#v, %v", actor, got, err)
		}
	}

	for _, principal := range []*action_servicepb.Principal{
		{},
		{Principal: &action_servicepb.Principal_Caller{Caller: &sandboxespb.ServiceAccount{Namespace: "", Name: "test-caller"}}},
		{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: "issuer"}}},
	} {
		if _, err := ActorFromProto(principal); !errors.Is(err, ErrInvalidActor) {
			t.Errorf("ActorFromProto(%v) error = %v, want ErrInvalidActor", principal, err)
		}
	}
	if _, err := ActorToProto(CallerActor{}); !errors.Is(err, ErrInvalidActor) {
		t.Errorf("ActorToProto(empty caller) error = %v, want ErrInvalidActor", err)
	}
}

func TestServiceAccountPreservesItsStrictStoredJSON(t *testing.T) {
	input := []byte(`{"namespace":"test-namespace","name":"test-caller"}`)
	var account ServiceAccount
	if err := json.Unmarshal(input, &account); err != nil {
		t.Fatal(err)
	}
	written, err := json.Marshal(account)
	if err != nil {
		t.Fatal(err)
	}
	if string(written) != string(input) {
		t.Errorf("ServiceAccount JSON = %s, want %s", written, input)
	}
	for _, input := range []string{
		`{"namespace":"test-namespace"}`,
		`{"namespace":null,"name":"test-caller"}`,
		`{"namespace":"","name":"test-caller"}`,
		`{"namespace":"test-namespace","name":"test-caller","extra":null}`,
	} {
		var account ServiceAccount
		if err := json.Unmarshal([]byte(input), &account); err == nil {
			t.Errorf("accepted %s", input)
		}
	}
}
