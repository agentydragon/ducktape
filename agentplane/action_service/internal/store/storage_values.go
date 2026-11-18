package store

import (
	"errors"
	"fmt"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
)

// The `action_decision.provider` values: who decided, which also fixes which of the decider columns are set.
const (
	operatorProvider = "human_operator"
	policyProvider   = "action_policy_set"
)

// The stored `action_decision.reason_code` values of a policy Decision.
var storedReasonCodes = [...]struct {
	code action_servicepb.PolicyDecision_ReasonCode
	text string
}{
	{action_servicepb.PolicyDecision_REASON_CODE_AUTO_APPROVE, "policy_set_auto_approve"},
}

// reasonCodeFromStored reads a stored reason code. A code this release does not name was recorded by a newer one, so it
// reads as REASON_CODE_UNKNOWN rather than failing the whole request.
func reasonCodeFromStored(text string) action_servicepb.PolicyDecision_ReasonCode {
	for _, known := range storedReasonCodes {
		if text == known.text {
			return known.code
		}
	}
	return action_servicepb.PolicyDecision_REASON_CODE_UNKNOWN
}

func reasonCodeStored(code action_servicepb.PolicyDecision_ReasonCode) (string, bool) {
	for _, known := range storedReasonCodes {
		if code == known.code {
			return known.text, true
		}
	}
	return "", false
}

// errInvalidActor marks a well-formed protobuf actor whose identity is incomplete.
var errInvalidActor = errors.New("invalid Action actor")

func (a ServiceAccount) validate() error {
	if a.Namespace == "" || a.Name == "" {
		return errors.New("a ServiceAccount needs a namespace and a name")
	}
	return nil
}

func serviceAccountFromProto(account *identitypb.ServiceAccount) (ServiceAccount, error) {
	if account == nil {
		return ServiceAccount{}, errors.New("a ServiceAccount needs a namespace and a name")
	}
	serviceAccount := ServiceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}
	if err := serviceAccount.validate(); err != nil {
		return ServiceAccount{}, err
	}
	return serviceAccount, nil
}

func (a ServiceAccount) Proto() *identitypb.ServiceAccount {
	return &identitypb.ServiceAccount{Namespace: a.Namespace, Name: a.Name}
}

func (i Identity) validate() error {
	if i.Name == "" || i.UID == "" {
		return errors.New("an Identity needs a name and a UID")
	}
	return nil
}

func (i Identity) Proto() *identitypb.Identity {
	return &identitypb.Identity{Name: i.Name, Uid: i.UID}
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
	case *action_servicepb.Principal_CallerIdentity:
		if principal.CallerUid != nil {
			return fmt.Errorf("%w: an Identity actor carries no ServiceAccount UID", errInvalidActor)
		}
		if err := (Identity{Name: identity.CallerIdentity.GetName(), UID: identity.CallerIdentity.GetUid()}).validate(); err != nil {
			return fmt.Errorf("invalid Identity actor: %w: %v", errInvalidActor, err)
		}
	case *action_servicepb.Principal_Operator:
		if identity.Operator == nil || identity.Operator.GetIssuer() == "" || identity.Operator.GetSubject() == "" {
			return fmt.Errorf("%w: an operator needs an issuer and a subject", errInvalidActor)
		}
	default:
		return fmt.Errorf("%w: principal has no caller, Identity or operator", errInvalidActor)
	}
	return nil
}
