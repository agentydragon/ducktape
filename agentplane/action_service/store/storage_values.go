package store

import (
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// The Action Service stores states as these SQL text values. Keep the mapping explicit: protobuf enum names
// and numbers are not a storage format.
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

func stateFromStored(text string) (action_servicepb.ActionState, bool) {
	for _, known := range storedStates {
		if text == known.text {
			return known.state, true
		}
	}
	return action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED, false
}

func stateStored(state action_servicepb.ActionState) (string, bool) {
	for _, known := range storedStates {
		if state == known.state {
			return known.text, true
		}
	}
	return "", false
}

func storedStateNames() []string {
	states := make([]string, len(storedStates))
	for i, known := range storedStates {
		states[i] = known.text
	}
	return states
}

// errInvalidActor marks a well-formed protobuf actor whose identity is incomplete.
var errInvalidActor = errors.New("invalid Action actor")

func (a ServiceAccount) validate() error {
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
	return a.validate()
}

func serviceAccountFromProto(account *sandboxespb.ServiceAccount) (ServiceAccount, error) {
	if account == nil {
		return ServiceAccount{}, errors.New("a ServiceAccount needs a namespace and a name")
	}
	serviceAccount := ServiceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}
	if err := serviceAccount.validate(); err != nil {
		return ServiceAccount{}, err
	}
	return serviceAccount, nil
}

func (a ServiceAccount) Proto() *sandboxespb.ServiceAccount {
	return &sandboxespb.ServiceAccount{Namespace: a.Namespace, Name: a.Name}
}

// validateActor validates a recorded identity. It grants no read or decision authority.
func validateActor(principal *action_servicepb.Principal) error {
	if principal == nil {
		return nil
	}
	switch identity := principal.GetPrincipal().(type) {
	case *action_servicepb.Principal_Caller:
		if _, err := serviceAccountFromProto(identity.Caller); err != nil {
			return fmt.Errorf("invalid caller actor: %w: %v", errInvalidActor, err)
		}
	case *action_servicepb.Principal_Operator:
		if identity.Operator == nil || identity.Operator.GetIssuer() == "" || identity.Operator.GetSubject() == "" {
			return fmt.Errorf("%w: an operator needs an issuer and a subject", errInvalidActor)
		}
	default:
		return fmt.Errorf("%w: principal has no caller or operator", errInvalidActor)
	}
	return nil
}
