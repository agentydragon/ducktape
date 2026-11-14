package dispatcher

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

// ServiceCaller is an exact, method-admitted component. It carries no Kubernetes account.
type ServiceCaller struct{ SPIFFEID string }

func (ServiceCaller) isCaller() {}

// IdentityServiceDelegation proves an effective Identity through a service certificate and Action envelope.
type IdentityServiceDelegation struct {
	ActorSPIFFEID string
	Action        ActionCorrelation
}

func (IdentityServiceDelegation) isIdentityProof() {}

// ServiceAttribution retains certificate actor proof independently of the effective Identity.
// An absent Identity denotes the component's own direct authority.
type ServiceAttribution struct {
	ActorSPIFFEID string               `json:"actor_spiffe_id"`
	Identity      *identitypb.Identity `json:"identity,omitempty"`
	Action        *ActionCorrelation   `json:"action,omitempty"`
}

func (*ServiceAttribution) isAttribution() {}

func isDelegatedIdentityProof(proof IdentityProof) bool {
	switch proof.(type) {
	case IdentityDelegation, IdentityServiceDelegation:
		return true
	default:
		return false
	}
}

func validateServiceAttribution(a *ServiceAttribution) error {
	if err := auth.ValidateServiceCallers([]string{a.ActorSPIFFEID}); err != nil {
		return err
	}
	if (a.Identity == nil) != (a.Action == nil) {
		return errors.New("service delegation requires an Identity and Action together")
	}
	if a.Identity != nil && (a.Identity.GetName() == "" || a.Identity.GetUid() == "" || a.Action.RequestID == "" || a.Action.ExecutionID == "") {
		return errors.New("service delegation attribution is incomplete")
	}
	return nil
}

func serviceAttributionBytes(recorded Attribution) (any, error) {
	a, ok := recorded.(*ServiceAttribution)
	if !ok {
		return nil, nil
	}
	if err := validateServiceAttribution(a); err != nil {
		return nil, err
	}
	data, err := json.Marshal(a)
	if err != nil {
		return nil, err
	}
	return string(data), nil
}

func scanServiceAttribution(existing Attribution, data []byte) (Attribution, error) {
	if data == nil {
		return existing, nil
	}
	if existing != nil {
		return nil, errors.New("service actor and legacy attribution cannot coexist")
	}
	a := new(ServiceAttribution)
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(a); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("trailing service attribution")
	}
	if err := validateServiceAttribution(a); err != nil {
		return nil, err
	}
	return a, nil
}
