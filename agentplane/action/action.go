// Package action holds the shared vocabulary and wire codecs for Action requests.
package action

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// State is where an Action request is in its lifecycle.
type State string

const (
	StateDecisionPending  State = "decision_pending"
	StateAllowed          State = "allowed"
	StateDenied           State = "denied"
	StateDispatching      State = "dispatching"
	StateRunning          State = "running"
	StateSucceeded        State = "succeeded"
	StateFailed           State = "failed"
	StateCancelled        State = "cancelled"
	StateExecutionUnknown State = "execution_unknown"
)

var stateViews = [...]struct {
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

// ErrInvalidActor marks a well-shaped actor whose identity is incomplete. Storage owners can classify this
// semantic error without treating malformed JSON or unknown fields as an invalid actor value.
var ErrInvalidActor = errors.New("invalid Action actor")

func (s State) Valid() bool {
	for _, known := range stateViews {
		if s == known.state {
			return true
		}
	}
	return false
}

// States returns the states in their stable protobuf vocabulary order.
func States() []State {
	states := make([]State, len(stateViews))
	for i, known := range stateViews {
		states[i] = known.state
	}
	return states
}

// StateFromProto reads only the Action Service's defined states. Unspecified and future values remain unknown.
func StateFromProto(state action_servicepb.ActionState) (State, bool) {
	for _, known := range stateViews {
		if known.proto == state {
			return known.state, true
		}
	}
	return "", false
}

// Proto writes only the Action Service's defined states. A future stored state has no wire form yet.
func (s State) Proto() (action_servicepb.ActionState, bool) {
	for _, known := range stateViews {
		if s == known.state {
			return known.proto, true
		}
	}
	return action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED, false
}

// ServiceAccount is a Kubernetes account that owns or authors an Action.
type ServiceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func (a ServiceAccount) Validate() error {
	if a.Namespace == "" || a.Name == "" {
		return errors.New("a ServiceAccount needs a namespace and a name")
	}
	return nil
}

func (a *ServiceAccount) UnmarshalJSON(data []byte) error {
	type plain ServiceAccount
	if err := decodeFields(data, (*plain)(a), "namespace", "name"); err != nil {
		return err
	}
	return a.Validate()
}

func ServiceAccountFromProto(account *sandboxespb.ServiceAccount) (ServiceAccount, error) {
	if account == nil {
		return ServiceAccount{}, errors.New("a ServiceAccount needs a namespace and a name")
	}
	serviceAccount := ServiceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}
	if err := serviceAccount.Validate(); err != nil {
		return ServiceAccount{}, err
	}
	return serviceAccount, nil
}

func (a ServiceAccount) Proto() *sandboxespb.ServiceAccount {
	return &sandboxespb.ServiceAccount{Namespace: a.Namespace, Name: a.Name}
}

// Actor is who authored a recorded Action event. It grants no read or decision authority.
type Actor interface{ isActor() }

// CallerActor is the ServiceAccount that requested a cancellation.
type CallerActor struct {
	Account ServiceAccount `json:"account"`
}

// OperatorActor is the verified human who requested a cancellation.
type OperatorActor struct {
	Issuer  string `json:"issuer"`
	Subject string `json:"subject"`
}

func (CallerActor) isActor()   {}
func (OperatorActor) isActor() {}

func DecodeActor(data []byte) (Actor, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, err
	}
	if _, caller := fields["account"]; caller {
		var wire struct {
			Account json.RawMessage `json:"account"`
		}
		if err := decodeFields(data, &wire, "account"); err != nil {
			return nil, err
		}
		type plainServiceAccount ServiceAccount
		var account plainServiceAccount
		if err := decodeFields(wire.Account, &account, "namespace", "name"); err != nil {
			return nil, err
		}
		caller := ServiceAccount(account)
		if err := caller.Validate(); err != nil {
			return nil, fmt.Errorf("invalid caller actor: %w: %v", ErrInvalidActor, err)
		}
		actor := CallerActor{Account: caller}
		return actor, nil
	}
	var actor OperatorActor
	if err := decodeFields(data, &actor, "issuer", "subject"); err != nil {
		return nil, err
	}
	if actor.Issuer == "" || actor.Subject == "" {
		return nil, fmt.Errorf("%w: an operator needs an issuer and a subject", ErrInvalidActor)
	}
	return actor, nil
}

func ActorFromProto(principal *action_servicepb.Principal) (Actor, error) {
	if principal == nil {
		return nil, nil
	}
	if caller := principal.GetCaller(); caller != nil {
		account, err := ServiceAccountFromProto(caller)
		if err != nil {
			return nil, fmt.Errorf("invalid caller actor: %w: %v", ErrInvalidActor, err)
		}
		return CallerActor{Account: account}, nil
	}
	if operator := principal.GetOperator(); operator != nil {
		if operator.GetIssuer() == "" || operator.GetSubject() == "" {
			return nil, fmt.Errorf("%w: an operator needs an issuer and a subject", ErrInvalidActor)
		}
		return OperatorActor{Issuer: operator.GetIssuer(), Subject: operator.GetSubject()}, nil
	}
	return nil, fmt.Errorf("%w: principal has no caller or operator", ErrInvalidActor)
}

func ActorToProto(actor Actor) (*action_servicepb.Principal, error) {
	switch actor := actor.(type) {
	case nil:
		return nil, nil
	case CallerActor:
		if err := actor.Account.Validate(); err != nil {
			return nil, fmt.Errorf("invalid caller actor: %w: %v", ErrInvalidActor, err)
		}
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: actor.Account.Proto()}}, nil
	case OperatorActor:
		if actor.Issuer == "" || actor.Subject == "" {
			return nil, fmt.Errorf("%w: an operator needs an issuer and a subject", ErrInvalidActor)
		}
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{
			Operator: &action_servicepb.Operator{Issuer: actor.Issuer, Subject: actor.Subject},
		}}, nil
	default:
		return nil, fmt.Errorf("unknown Action actor %T", actor)
	}
}

func decodeFields(data []byte, v any, required ...string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for _, name := range required {
		value, present := fields[name]
		if !present || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return fmt.Errorf("%s is required", name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(v)
}
