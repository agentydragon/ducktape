package store

import (
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestDelegationAttributionRoundTripsBesideEffectiveCaller(t *testing.T) {
	f := newFixture(t)
	owner := IdentityPrincipal{Identity: Identity{Name: "test-effective-identity", UID: "test-effective-identity-uid"}}
	sessionID, connectionID, grantID := uuid.New(), uuid.New(), uuid.New()
	serviceAccountActor := func(podName, podUID string) *action_servicepb.DelegatingServiceAccount {
		return &action_servicepb.DelegatingServiceAccount{
			Identity: &identitypb.ServiceAccountIdentity{
				Account: &identitypb.ServiceAccount{Namespace: "test-actor-namespace", Name: "test-actor"}, Uid: "test-actor-uid",
			},
			PodName: podName, PodUid: podUID,
		}
	}
	for _, test := range []struct {
		key         string
		attribution *DelegationAttribution
	}{
		{
			key: "session-service-actor",
			attribution: &DelegationAttribution{
				Actor: action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceSpiffeId{
					ServiceSpiffeId: "spiffe://test.example/delegator",
				}},
				Provenance: SessionProvenance{SessionID: sessionID},
			},
		},
		{key: "session-workload-actor-without-pod-binding", attribution: &DelegationAttribution{
			Actor: action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceAccount{ServiceAccount: serviceAccountActor("", "")}}, Provenance: SessionProvenance{SessionID: sessionID},
		}},
		{key: "connection-workload-actor", attribution: &DelegationAttribution{
			Actor:      action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceAccount{ServiceAccount: serviceAccountActor("test-actor-pod", "test-actor-pod-uid")}},
			Provenance: ConnectionProvenance{ConnectionID: connectionID, GrantID: grantID},
		}},
	} {
		t.Run(test.key, func(t *testing.T) {
			request := f.submitInput(t, owner, input(test.key), Admission{RequestID: uuid.New(), Attribution: test.attribution})
			if !sameAttribution(request.Attribution, test.attribution) || request.Caller != (IdentityCaller{Identity: owner.Identity}) {
				t.Fatalf("submitted request = caller %T/%+v attribution %+v", request.Caller, request.Caller, request.Attribution)
			}
			read := f.get(t, request.ID)
			if !sameAttribution(read.Attribution, test.attribution) || read.Caller != (IdentityCaller{Identity: owner.Identity}) {
				t.Fatalf("read request = caller %T/%+v attribution %+v", read.Caller, read.Caller, read.Attribution)
			}
		})
	}

	first, err := f.List(t.Context(), owner, ListFilter{IdempotencyKey: stringPointer("session-service-actor")})
	if err != nil || len(first) != 1 {
		t.Fatalf("read by effective Identity and idempotency key = %+v, %v", first, err)
	}
	connectionAttribution := &DelegationAttribution{
		Actor:      action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceSpiffeId{ServiceSpiffeId: "spiffe://test.example/other"}},
		Provenance: ConnectionProvenance{ConnectionID: connectionID, GrantID: grantID},
	}
	if _, err := f.Submit(t.Context(), owner, input("session-service-actor"), Admission{RequestID: uuid.New(), Attribution: connectionAttribution}); conflictReason(err) != ConflictIdempotencyKeyUsed {
		t.Fatalf("attribution changed idempotency behavior: %v", err)
	}
	first, err = f.List(t.Context(), owner, ListFilter{IdempotencyKey: stringPointer("session-service-actor")})
	if err != nil || len(first) != 1 || !sameAttribution(first[0].Attribution, &DelegationAttribution{
		Actor:      action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceSpiffeId{ServiceSpiffeId: "spiffe://test.example/delegator"}},
		Provenance: SessionProvenance{SessionID: sessionID},
	}) {
		t.Fatalf("original attribution after conflicting retry = %+v, %v", first, err)
	}
}

func TestDirectAndLegacyWritersReadWithoutAttribution(t *testing.T) {
	f := newFixture(t)
	direct := f.submit(t, caller, "direct-without-attribution")
	if direct.Attribution != nil || f.get(t, direct.ID).Attribution != nil {
		t.Fatalf("direct request attribution = %+v", direct.Attribution)
	}

	legacyID := uuid.New()
	f.exec(t, `INSERT INTO action_request (id, idempotency_key, action_group, action_name, arguments, title,
		caller_namespace, caller_name, caller_uid, state, version, created_at, updated_at)
		VALUES ($1, 'legacy-writer', 'test-group', 'test-action', '{}', 'legacy receipt',
			'test-namespace', 'legacy-caller', 'legacy-caller-uid', 'decision_pending', 1, now(), now())`, legacyID)
	legacy := f.get(t, legacyID)
	recorded, ok := legacy.Caller.(ServiceAccountCaller)
	if legacy.Attribution != nil || !ok || recorded.Account != (ServiceAccount{Namespace: "test-namespace", Name: "legacy-caller"}) ||
		recorded.UID == nil || *recorded.UID != "legacy-caller-uid" {
		t.Fatalf("legacy request = caller %+v attribution %+v", legacy.Caller, legacy.Attribution)
	}
}

func TestInvalidDelegationAttributionStoresNothing(t *testing.T) {
	f := newFixture(t)
	sessionID := uuid.New()
	for _, test := range []struct {
		name        string
		attribution *DelegationAttribution
	}{
		{name: "missing actor", attribution: &DelegationAttribution{Provenance: SessionProvenance{SessionID: sessionID}}},
		{name: "missing provenance", attribution: &DelegationAttribution{
			Actor: action_servicepb.DelegatingActor{Actor: &action_servicepb.DelegatingActor_ServiceSpiffeId{ServiceSpiffeId: "spiffe://test.example/delegator"}},
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := f.Submit(t.Context(), caller, input("invalid-"+test.name), Admission{
				RequestID: uuid.New(), Attribution: test.attribution,
			}); err == nil {
				t.Fatal("invalid attribution was stored")
			}
		})
	}
	if got := f.count(t, `SELECT count(*) FROM action_request`); got != 0 {
		t.Fatalf("invalid attributions left %d requests", got)
	}
}

func sameAttribution(got, want *DelegationAttribution) bool {
	if got == nil || want == nil {
		return got == want
	}
	return proto.Equal(&got.Actor, &want.Actor) && reflect.DeepEqual(got.Provenance, want.Provenance)
}

func stringPointer(value string) *string { return &value }
