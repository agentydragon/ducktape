package dispatcher

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environmentaccess"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
)

// A platform component acting for an Identity, through the delegated client the Action Service
// uses, acts on exactly the Sessions recorded with that live Identity incarnation, as the
// Identity's own workload would, and opens none. The actor being a privileged Dispatcher caller
// widens nothing.
func TestDelegatedIdentityActsOnlyOnItsIdentitysSessions(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	const policyNamespace = "test-delegated-identity-policies"
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "delegated-identity-owner", policyNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "delegated-identity-other", policyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	otherIdentity := &identitypb.Identity{Name: other.Identity.Name, Uid: string(other.Identity.UID)}
	ownerAccount := serviceAccountIdentityFor(owner.Principal.Account, owner.Principal.ServiceAccountUID)
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox, identity: ownerIdentity}
	rules := environmentaccess.NewIndex()
	rules.PutIdentity(owner.Identity)
	rules.PutIdentity(other.Identity)
	var policyError error

	actor := auth.Account{Namespace: caller.Namespace, Name: "test-delegating-actions"}
	resolver, err := auth.NewResolver(tokenReviewStub{"test-actions-token": {account: actor, uid: "test-actions-uid", pod: true}},
		authorityTestAudience, []string{caller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	identities, err := auth.KubernetesIdentityUIDs(owner.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	var delegated []string
	for method := range Methods(auth.Rule{}) {
		delegated = append(delegated, method)
	}
	methods, err := MethodsWithDelegation(resolver, auth.Delegator{Account: actor, IdentityCallers: true}, identities, delegated)
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 10)
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, connected: true}
	server := grpc.NewServer(methods.ServerOptions(time.Second)...)
	dispatcherpb.RegisterSessionDispatcherServer(server, &Service{
		Store: store, DispatcherCallers: sets.New(actor), Sandboxes: inventory,
		EnvironmentRules: rules, PolicyReady: func() error { return policyError },
		Hub: hub, WatchLease: 10 * time.Second,
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-actions-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	connection, err := workloadgrpc.DialDelegated(listener.Addr().String(), tokenFile, 10*time.Second, nil, workloadgrpc.ClientInterceptors{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	client := dispatcherpb.NewSessionDispatcherClient(connection)
	requestID, executionID := uuid.NewString(), uuid.NewString()
	actingFor := func(identity *identitypb.Identity) context.Context {
		t.Helper()
		ctx, err := workloadgrpc.WithDelegation(t.Context(), &delegationpb.Delegation{
			EffectiveCaller: &delegationpb.Delegation_Identity{Identity: identity},
			Provenance:      &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: requestID, ExecutionId: executionID}},
		})
		if err != nil {
			t.Fatal(err)
		}
		return ctx
	}

	seed := func(sandbox SandboxSnapshot, execution SessionExecution, creator Caller) string {
		t.Helper()
		sessionID, spec := uuid.NewString(), &sessionpb.SessionSpec{}
		testPlacement(spec, sandbox.Name, sandbox.UID)
		if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), sessionID, sandbox.Name, sandbox.UID,
			spec, nil, nil, execution, creator); err != nil {
			t.Fatalf("seed Session: %v", err)
		}
		return sessionID
	}
	owned := seed(ownerSandbox, SessionExecution{Identity: ownerIdentity},
		IdentityCaller{Identity: ownerIdentity, Proof: IdentitySVID{}})
	otherSandbox := SandboxSnapshot{Name: other.Sandbox.Name, Namespace: other.Sandbox.Namespace, UID: string(other.Sandbox.UID)}
	otherSession := seed(otherSandbox, SessionExecution{Identity: otherIdentity},
		IdentityCaller{Identity: otherIdentity, Proof: IdentitySVID{}})
	accountSession := seed(ownerSandbox, SessionExecution{Identity: otherIdentity},
		ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: ownerAccount, Attribution: &AdmissionAttribution{Actor: ownerAccount, EffectiveCaller: ownerAccount}})

	const command = "delegated-identity-input"
	if _, err := client.SubmitInput(actingFor(ownerIdentity), &dispatcherpb.SubmitInputRequest{
		CommandId: command, SessionId: owned, ExpectedSandboxUid: ownerSandbox.UID, Text: "owned",
	}); err != nil {
		t.Fatalf("delegated Identity SubmitInput: %v", err)
	}
	queued, err := store.GetDispatch(t.Context(), command)
	if err != nil {
		t.Fatal(err)
	}
	want := &DelegatedIdentityAttribution{Identity: ownerIdentity, Delegation: IdentityDelegation{
		Actor:    serviceAccountIdentityFor(actor, "test-actions-uid"),
		ActorPod: PodIdentity{Name: "dispatcher-backend", UID: "dispatcher-backend-uid"},
		Action:   ActionCorrelation{RequestID: requestID, ExecutionID: executionID},
	}}
	recorded, ok := queued.Attribution.(*DelegatedIdentityAttribution)
	if !ok || !proto.Equal(recorded.Identity, want.Identity) || !proto.Equal(recorded.Delegation.Actor, want.Delegation.Actor) ||
		recorded.Delegation.ActorPod != want.Delegation.ActorPod || recorded.Delegation.Action != want.Delegation.Action {
		t.Fatalf("Dispatch attributed to %#v, want %#v", queued.Attribution, want)
	}
	if got, err := client.GetSession(actingFor(ownerIdentity), &dispatcherpb.GetSessionRequest{SessionId: owned}); err != nil || got.GetSession().GetSessionId() != owned {
		t.Fatalf("delegated Identity GetSession: %v, %v", got, err)
	}
	if got, err := client.GetDispatch(actingFor(ownerIdentity), &dispatcherpb.GetDispatchRequest{CommandId: command}); err != nil || got.GetDispatch().GetCommandId() != command {
		t.Fatalf("delegated Identity GetDispatch: %v, %v", got, err)
	}
	listed, err := client.ListSessions(actingFor(ownerIdentity), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.GetSessions()[0].GetSessionId() != owned {
		t.Fatalf("delegated Identity ListSessions = %v, %v; want only its Session", listed, err)
	}
	for _, foreign := range []string{otherSession, accountSession} {
		if _, err := client.GetSession(actingFor(ownerIdentity), &dispatcherpb.GetSessionRequest{SessionId: foreign}); status.Code(err) != codes.NotFound {
			t.Fatalf("delegated Identity read a Session it does not own: %v", err)
		}
		if _, err := client.SubmitInput(actingFor(ownerIdentity), &dispatcherpb.SubmitInputRequest{
			CommandId: uuid.NewString(), SessionId: foreign, ExpectedSandboxUid: ownerSandbox.UID, Text: "foreign",
		}); status.Code(err) != codes.NotFound {
			t.Fatalf("delegated Identity wrote to a Session it does not own: %v", err)
		}
	}
	if _, err := client.CancelDispatch(actingFor(otherIdentity), &dispatcherpb.CancelDispatchRequest{CommandId: command}); status.Code(err) != codes.NotFound {
		t.Fatalf("acting for another Identity cancelled the Session's command: %v", err)
	}
	if _, err := client.CreateSession(actingFor(ownerIdentity), &dispatcherpb.CreateSessionRequest{
		SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: ownerSandbox.Name, Uid: ownerSandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}},
	}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated Identity CreateSession on its own Sandbox: %v, want PermissionDenied", err)
	}
	// A different Identity can create on this Sandbox only under a grant naming its bound Identity.
	rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "delegated-bind-session"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "environment", Operations: []string{"bind_session"}, Environments: []policyapi.EnvironmentSelector{{
			BoundIdentity: &policyapi.BoundIdentitySelector{Name: ownerIdentity.GetName()},
		}}}}},
	})
	rules.PutBinding(&policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: "delegated-bind-session"}, Spec: policyapi.PolicyBindingSpec{
		Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: otherIdentity.GetName()}}, PolicySets: []string{"delegated-bind-session"},
	}})
	createdID := uuid.NewString()
	createAsOther := &dispatcherpb.CreateSessionRequest{SessionId: createdID, Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: ownerSandbox.Name, Uid: ownerSandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}}
	created, err := client.CreateSession(actingFor(otherIdentity), createAsOther)
	if err != nil || created.GetSession().GetIdentity().GetName() != ownerIdentity.GetName() {
		t.Fatalf("delegated named bind_session create = %v, %v", created, err)
	}
	createdRow, err := store.GetSession(t.Context(), createdID)
	if err != nil || !proto.Equal(createdRow.ExecutionIdentity, ownerIdentity) {
		t.Fatalf("delegated created Session executes as %v, %v", createdRow, err)
	}
	creator, ok := createdRow.Attribution.(*DelegatedIdentityAttribution)
	if !ok || !proto.Equal(creator.Identity, otherIdentity) || creator.Delegation.Actor.GetAccount().GetName() != actor.Name ||
		creator.Delegation.Action.RequestID != requestID || creator.Delegation.Action.ExecutionID != executionID {
		t.Fatalf("delegated creator provenance = %#v", createdRow.Attribution)
	}
	if _, err := client.CreateSession(actingFor(ownerIdentity), createAsOther); status.Code(err) != codes.NotFound {
		t.Fatalf("non-creator recovered another Identity's CreateSession receipt: %v", err)
	}
	altered := proto.Clone(createAsOther).(*dispatcherpb.CreateSessionRequest)
	changedSetup := "different setup"
	altered.SetupScript = &changedSetup
	if _, err := client.CreateSession(actingFor(otherIdentity), altered); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("creator altered the retained CreateSession request: %v", err)
	}
	if _, err := client.GetSession(actingFor(otherIdentity), &dispatcherpb.GetSessionRequest{SessionId: createdID}); status.Code(err) != codes.NotFound {
		t.Fatalf("creator received Session ownership from receipt authority: %v", err)
	}
	rules.DeleteBinding("delegated-bind-session")
	policyError = errors.New("environment rule watch unavailable")
	inventory.mu.Lock()
	inventory.sandbox = SandboxSnapshot{Name: "replacement-sandbox", Namespace: ownerSandbox.Namespace, UID: uuid.NewString()}
	inventory.mu.Unlock()
	if _, err := client.CreateSession(actingFor(otherIdentity), createAsOther); err != nil {
		t.Fatalf("exact creator receipt retry after revocation and target loss: %v", err)
	}
	inventory.mu.Lock()
	inventory.sandbox = ownerSandbox
	inventory.mu.Unlock()
	if _, err := client.CancelDispatch(actingFor(ownerIdentity), &dispatcherpb.CancelDispatchRequest{CommandId: command}); err != nil {
		t.Fatalf("delegated Identity CancelDispatch of its own command: %v", err)
	}

	// A same-named successor is another Identity: the recorded incarnation's delegation is refused.
	if err := owner.Objects.Delete(t.Context(), owner.Identity); err != nil {
		t.Fatal(err)
	}
	successor := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: owner.Identity.Name},
		Spec:       policyapi.IdentitySpec{Description: "successor of " + owner.Identity.Name},
	}
	if err := owner.Objects.Create(t.Context(), successor); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetSession(actingFor(ownerIdentity), &dispatcherpb.GetSessionRequest{SessionId: owned}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegation naming a replaced Identity incarnation: %v, want PermissionDenied", err)
	}
	if _, err := client.GetSession(actingFor(&identitypb.Identity{Name: successor.Name, Uid: string(successor.UID)}),
		&dispatcherpb.GetSessionRequest{SessionId: owned}); status.Code(err) != codes.NotFound {
		t.Fatalf("the successor read its predecessor's Session: %v, want NotFound", err)
	}
}
