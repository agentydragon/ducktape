package store

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

func TeststoredStateNamesRoundTrip(t *testing.T) {
	pairs := []struct {
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
	for _, pair := range pairs {
		state, ok := stateFromStored(pair.text)
		if !ok || state != pair.state {
			t.Errorf("stored %q decoded as %v, %v; want %v", pair.text, state, ok, pair.state)
		}
		if got, ok := stateStored(pair.state); !ok || got != pair.text {
			t.Errorf("state %v stored as %q, %v; want %q", pair.state, got, ok, pair.text)
		}
	}
	if len(storedStateNames()) != len(pairs) {
		t.Errorf("stored vocabulary has %d states, want %d", len(storedStateNames()), len(pairs))
	}
	for _, state := range []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED, 99} {
		if _, ok := stateStored(state); ok {
			t.Errorf("accepted state %v", state)
		}
	}
	if _, ok := stateFromStored("future_state"); ok {
		t.Error("accepted unknown stored state")
	}
}

func TestValidateActor(t *testing.T) {
	if err := validateActor(nil); err != nil {
		t.Errorf("an absent actor is invalid: %v", err)
	}
	for _, actor := range []*action_servicepb.Principal{
		{Principal: &action_servicepb.Principal_Caller{Caller: &sandboxespb.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}}},
		{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: "https://issuer.example", Subject: "subject-123"}}},
	} {
		if err := validateActor(actor); err != nil {
			t.Errorf("validateActor(%v) = %v", actor, err)
		}
	}
	for _, actor := range []*action_servicepb.Principal{
		{},
		{Principal: &action_servicepb.Principal_Caller{Caller: &sandboxespb.ServiceAccount{Name: "test-caller"}}},
		{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: "issuer"}}},
	} {
		if err := validateActor(actor); !errors.Is(err, errInvalidActor) {
			t.Errorf("validateActor(%v) = %v", actor, err)
		}
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
