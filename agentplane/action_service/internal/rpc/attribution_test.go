package rpc

import (
	"encoding/json"
	"testing"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestSessionAttributionUsesOnlyTheAdmittedServiceAndSession(t *testing.T) {
	id := uuid.New()
	identity := store.Identity{Name: "central-session-identity", UID: "central-session-identity-uid"}
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: identity.Proto()},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: id.String()}},
	}
	recorded, err := recordedDelegation(auth.DelegatedServiceIdentityAdmission{
		Actor:           auth.ServicePrincipal{SPIFFEID: peertls.CentralSamplerURI},
		EffectiveCaller: auth.Identity{Name: identity.Name, UID: identity.UID}, Provenance: envelope,
	})
	if err != nil {
		t.Fatal(err)
	}
	if recorded.Provenance != (store.SessionProvenance{SessionID: id}) || recorded.Actor.GetServiceSpiffeId() != peertls.CentralSamplerURI {
		t.Fatalf("admitted Session attribution = %+v", recorded)
	}
	// The receipt needs only the logical Session and exact Identity. No environment is involved,
	// and caller-authored fields do not contribute to its authenticated attribution.
	request := store.ActionRequest{
		ID: uuid.New(), Arguments: json.RawMessage(`{"delegating_actor":"forged","session_id":"forged"}`),
		Caller: store.IdentityCaller{Identity: identity}, Attribution: recorded,
		State: pb.ActionState_ACTION_STATE_DECISION_PENDING,
	}
	view, err := requestView(request)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(view.GetSessionProvenance(), envelope.GetSession()) || !proto.Equal(view.GetCallerIdentity(), identity.Proto()) || view.GetDelegatingActor().GetServiceSpiffeId() != peertls.CentralSamplerURI {
		t.Fatalf("Session receipt = %v", view)
	}
	request.Attribution = nil
	legacy, err := requestView(request)
	if err != nil || legacy.GetDelegatingActor() != nil || legacy.GetProvenance() != nil || !proto.Equal(legacy.GetCallerIdentity(), identity.Proto()) {
		t.Fatalf("absent attribution was inferred or refused: %v, %v", legacy, err)
	}
}
