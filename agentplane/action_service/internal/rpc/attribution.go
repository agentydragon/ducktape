package rpc

import (
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/google/uuid"
)

// recordedDelegation copies only authenticated actor proof and typed audit correlation. Request
// arguments never contribute attribution, and the caller's ordinary Identity remains its owner.
func recordedDelegation(proof auth.Admission) (*store.DelegationAttribution, error) {
	attribution := new(store.DelegationAttribution)
	var envelope *delegationpb.Delegation
	switch proof := proof.(type) {
	case auth.DelegatedServiceIdentityAdmission:
		attribution.Actor = action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceSpiffeId{ServiceSpiffeId: proof.Actor.SPIFFEID}}
		envelope = proof.Provenance
	default:
		return nil, unauthenticated
	}
	switch provenance := envelope.GetProvenance().(type) {
	case *delegationpb.Delegation_Session:
		id, err := uuid.Parse(provenance.Session.GetSessionId())
		if err != nil {
			return nil, unauthenticated
		}
		attribution.Provenance = store.SessionProvenance{SessionID: id}
	case *delegationpb.Delegation_Connection:
		connectionID, err := uuid.Parse(provenance.Connection.GetConnectionId())
		if err != nil {
			return nil, unauthenticated
		}
		grantID, err := uuid.Parse(provenance.Connection.GetGrantId())
		if err != nil {
			return nil, unauthenticated
		}
		attribution.Provenance = store.ConnectionProvenance{ConnectionID: connectionID, GrantID: grantID}
	default:
		return nil, unauthenticated
	}
	return attribution, nil
}
