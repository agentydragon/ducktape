package ingress

import (
	"context"
	"testing"

	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// openDelegated is a Push call a sampler makes as a delegating actor for identity, serving sessionID.
func (i testIngress) openDelegated(t *testing.T, token string, identity *identitypb.Identity, sessionID string) *call {
	t.Helper()
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: identity},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: sessionID}},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", auth.DelegatedScheme+" "+token, auth.DelegationMetadataKey, string(encoded))
	ctx, cancel := context.WithTimeout(ctx, callDeadline)
	t.Cleanup(cancel)
	stream, err := i.client.Push(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return &call{t: t, stream: stream, cancel: cancel}
}

// centralAnnouncement is a sampler's announcement of a central Session the Dispatcher recorded as
// executing as identity (nil: as no Identity).
func centralAnnouncement(sessionID string, identity *identitypb.Identity) *session_ingresspb.PushFrame {
	executorUID := uuid.NewString()
	return &session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: &session_ingresspb.SamplerAnnouncement{
		Session: &sessionpb.Attached{
			SessionId: sessionID,
			Spec: &sessionpb.SessionSpec{
				Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
					ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "test-execution-sandbox", Uid: executorUID},
				}}},
				Model: "test-model", Cwd: "/test-workspace",
			},
			ExecutionIdentity: identity,
		},
		Executor: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: testNamespace, Name: "test-execution-sandbox", Uid: executorUID},
			Owner:  &identitypb.ServiceAccount{Namespace: testNamespace, Name: "test-execution-sandbox"},
		},
	}}}
}

// A central sampler acting for the Identity a Session executes as stays the Session's writer, and
// the Identity, verified live, is retained as its execution Identity. It is refused for an Identity
// other than the Dispatcher recorded, a Session recorded as no Identity, a Session other than the
// one its envelope names, a native journal, an actor without the grant and an incarnation that no
// longer exists. The sampler pushing as itself keeps relaying the recorded Identity as before.
func TestADelegatingSamplerBindsTheSessionToTheIdentityItActsFor(t *testing.T) {
	const policyNamespace = "test-delegating-sampler-policies"
	service, db := newService(t, testTiming)
	sampler := auth.Account{Namespace: testNamespace, Name: "test-delegating-sampler"}
	service.SamplerAccounts = []auth.Account{sampler}
	named := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "test-central-session-identity", policyNamespace)
	identity := &identitypb.Identity{Name: named.Identity.Name, Uid: string(named.Identity.UID)}
	identities, err := auth.KubernetesIdentityUIDs(named.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	ingress := serveIngressAdmitting(t, service, func(principals *auth.Resolver) auth.Rule {
		rule, err := AllowDelegatingSamplers(auth.SVIDOrAccounts(principals, []auth.Account{sampler}), []auth.Account{sampler}, identities)
		if err != nil {
			t.Fatal(err)
		}
		return rule
	})
	token := workloadToken(t, sampler)

	sessionID := uuid.NewString()
	c := ingress.openDelegated(t, token, identity, sessionID)
	c.send(centralAnnouncement(sessionID, identity))
	c.acknowledged(0, false)
	var writerName, identityName, identityUID string
	if err := db.QueryRowContext(t.Context(), "SELECT writer_name, execution_identity_name, execution_identity_uid FROM session_history WHERE id=$1", sessionID).
		Scan(&writerName, &identityName, &identityUID); err != nil {
		t.Fatal(err)
	}
	if writerName != sampler.Name || identityName != identity.Name || identityUID != identity.Uid {
		t.Fatalf("binding writer=%s identity=%s/%s, want the sampler acting for %v", writerName, identityName, identityUID, identity)
	}
	c.cancel()

	relayed := uuid.NewString()
	plain := ingress.open(t, token)
	plain.send(centralAnnouncement(relayed, identity))
	plain.acknowledged(0, false)
	plain.cancel()

	refused := func(t *testing.T, c *call, frame *session_ingresspb.PushFrame, want codes.Code) {
		t.Helper()
		c.send(frame)
		if _, err := c.stream.Recv(); status.Code(err) != want {
			t.Fatalf("Recv = %v, want %v", err, want)
		}
	}
	t.Run("an Identity other than the Dispatcher recorded", func(t *testing.T) {
		other := uuid.NewString()
		refused(t, ingress.openDelegated(t, token, identity, other),
			centralAnnouncement(other, &identitypb.Identity{Name: identity.Name, Uid: uuid.NewString()}), codes.PermissionDenied)
	})
	t.Run("a Session recorded as no Identity", func(t *testing.T) {
		other := uuid.NewString()
		refused(t, ingress.openDelegated(t, token, identity, other), centralAnnouncement(other, nil), codes.PermissionDenied)
	})
	t.Run("a Session other than the envelope names", func(t *testing.T) {
		refused(t, ingress.openDelegated(t, token, identity, uuid.NewString()), centralAnnouncement(uuid.NewString(), identity), codes.PermissionDenied)
	})
	t.Run("a native journal", func(t *testing.T) {
		other := uuid.NewString()
		native := &session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_Announce{Announce: &sessionpb.Attached{
			SessionId: other, Spec: &sessionpb.SessionSpec{Model: "test-model", Cwd: "/test-workspace"}, ExecutionIdentity: identity,
		}}}
		refused(t, ingress.openDelegated(t, token, identity, other), native, codes.PermissionDenied)
	})
	t.Run("an actor without the grant", func(t *testing.T) {
		other := uuid.NewString()
		refused(t, ingress.openDelegated(t, writerToken, identity, other), centralAnnouncement(other, identity), codes.PermissionDenied)
	})
	t.Run("an incarnation that no longer exists", func(t *testing.T) {
		if err := named.Objects.Delete(t.Context(), named.Identity); err != nil {
			t.Fatal(err)
		}
		other := uuid.NewString()
		refused(t, ingress.openDelegated(t, token, identity, other), centralAnnouncement(other, identity), codes.PermissionDenied)
	})
}
