package storagecodec

import (
	"encoding/json"
	"errors"
	"fmt"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// Notification payloads store Action states as these legacy text values. Keep the mapping explicit: protobuf enum names
// and numbers are not a storage format.
var storedActionStates = [...]struct {
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

// ActionStateFromStored decodes the stored lifecycle state into its canonical Action enum.
func ActionStateFromStored(text string) (action_servicepb.ActionState, bool) {
	for _, known := range storedActionStates {
		if text == known.text {
			return known.state, true
		}
	}
	return action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED, false
}

func storedActionState(state action_servicepb.ActionState) (string, bool) {
	for _, known := range storedActionStates {
		if state == known.state {
			return known.text, true
		}
	}
	return "", false
}

var errInvalidActionActor = errors.New("invalid Action actor")

type actionActorIdentity struct {
	Name string `json:"name"`
	UID  string `json:"uid"`
}

type actionActorAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func (a *actionActorAccount) UnmarshalJSON(data []byte) error {
	var wire struct {
		Namespace *string `json:"namespace"`
		Name      *string `json:"name"`
	}
	if err := Decode(data, &wire); err != nil {
		return err
	}
	if err := errors.Join(Required("namespace", wire.Namespace != nil), Required("name", wire.Name != nil)); err != nil {
		return err
	}
	*a = actionActorAccount{Namespace: *wire.Namespace, Name: *wire.Name}
	if a.Namespace == "" || a.Name == "" {
		return errors.New("a ServiceAccount needs a namespace and a name")
	}
	return nil
}

func ServiceAccountProto(a model.ServiceAccount) *identitypb.ServiceAccount {
	return &identitypb.ServiceAccount{Namespace: a.Namespace, Name: a.Name}
}

// ValidateActionActor validates a recorded identity. It grants no read or decision authority.
func ValidateActionActor(principal *action_servicepb.Principal) error {
	if principal == nil {
		return nil
	}
	switch identity := principal.GetPrincipal().(type) {
	case *action_servicepb.Principal_Caller:
		account := identity.Caller
		if account == nil || account.GetNamespace() == "" || account.GetName() == "" {
			err := errors.New("a ServiceAccount needs a namespace and a name")
			return fmt.Errorf("invalid caller actor: %w: %v", errInvalidActionActor, err)
		}
	case *action_servicepb.Principal_CallerIdentity:
		if principal.CallerUid != nil || identity.CallerIdentity.GetName() == "" || identity.CallerIdentity.GetUid() == "" {
			return fmt.Errorf("%w: an Identity actor needs name and UID and no ServiceAccount UID", errInvalidActionActor)
		}
	case *action_servicepb.Principal_Operator:
		if identity.Operator == nil || identity.Operator.GetIssuer() == "" || identity.Operator.GetSubject() == "" {
			return fmt.Errorf("%w: an operator needs an issuer and a subject", errInvalidActionActor)
		}
	default:
		return fmt.Errorf("%w: principal has no caller, Identity or operator", errInvalidActionActor)
	}
	return nil
}

// EncodeActionActor writes the legacy flat JSON document. Nil remains JSON null.
func EncodeActionActor(principal *action_servicepb.Principal) ([]byte, error) {
	if principal == nil {
		return []byte("null"), nil
	}
	if err := ValidateActionActor(principal); err != nil {
		return nil, err
	}
	switch identity := principal.GetPrincipal().(type) {
	case *action_servicepb.Principal_Caller:
		return json.Marshal(struct {
			Account actionActorAccount `json:"account"`
		}{actionActorAccount{Namespace: identity.Caller.GetNamespace(), Name: identity.Caller.GetName()}})
	case *action_servicepb.Principal_CallerIdentity:
		return json.Marshal(struct {
			Identity actionActorIdentity `json:"identity"`
		}{actionActorIdentity{Name: identity.CallerIdentity.GetName(), UID: identity.CallerIdentity.GetUid()}})
	case *action_servicepb.Principal_Operator:
		return json.Marshal(struct {
			Issuer  string `json:"issuer"`
			Subject string `json:"subject"`
		}{identity.Operator.GetIssuer(), identity.Operator.GetSubject()})
	default:
		panic("validated principal has unknown variant")
	}
}
