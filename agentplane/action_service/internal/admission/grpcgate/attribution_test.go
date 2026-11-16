package grpcgate

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
)

func TestRecordedSessionAttribution(t *testing.T) {
	id := uuid.New()
	identity := auth.Identity{Name: "test-session-identity", UID: "test-session-uid"}
	envelope := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}}, Provenance: &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: id.String()}}}
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
}
