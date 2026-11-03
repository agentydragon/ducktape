package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	authorityTestAudience = "dispatcher-authority-test"
	authoritySandboxUID   = "00000000-0000-4000-8000-000000009013"
	pendingSandboxUID     = "00000000-0000-4000-8000-000000009014"
)

type reviewedToken struct {
	account auth.Account
	uid     string
	pod     bool
}

type tokenReviewStub map[string]reviewedToken

func (r tokenReviewStub) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	identity, found := r[request.Spec.Token]
	if !found {
		return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{}}, nil
	}
	extra := map[string]authenticationv1.ExtraValue{}
	if identity.pod {
		extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{"dispatcher-backend"}
		extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{"dispatcher-backend-uid"}
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: fmt.Sprintf("system:serviceaccount:%s:%s", identity.account.Namespace, identity.account.Name),
			UID:      identity.uid,
			Extra:    extra,
		},
	}}, nil
}

type authorityInventory struct {
	mu           sync.Mutex
	sandbox      SandboxSnapshot
	account      *ServiceAccountIdentity
	accountFound bool
	sandboxErr   error
}

func (i *authorityInventory) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.sandboxErr != nil {
		return SandboxSnapshot{}, false, i.sandboxErr
	}
	return i.sandbox, name == i.sandbox.Name, nil
}

func (i *authorityInventory) GetExecutionAccount(_ context.Context, sandbox SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if sandbox.Name != i.sandbox.Name || sandbox.UID != i.sandbox.UID {
		return nil, false, nil
	}
	return i.account, i.accountFound, nil
}

func (i *authorityInventory) setAccount(account *ServiceAccountIdentity) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.account, i.accountFound = account, true
}

func (i *authorityInventory) failSandboxRead(err error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.sandboxErr = err
}

func authorityClient(t *testing.T, store *Store, inventory *authorityInventory, reviews tokenReviewStub, hub *WatchHub) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, authorityTestAudience, []string{caller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Store: store, DispatcherCallers: configuredDispatcherCallers(),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	connection := grpcmethods.Serve(t, Methods(resolver), func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	})
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

func authorityContext(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func delegatedAuthorityContext(
	t *testing.T,
	token string,
	caller auth.Account,
	uid, actionRequestID, actionExecutionID string,
) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		Caller: &identitypb.ServiceAccountIdentity{
			Account: &identitypb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, Uid: uid,
		},
		ActionRequestId: actionRequestID, ActionExecutionId: actionExecutionID,
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token, auth.DelegationMetadataKey, string(encoded),
	))
}

func authorityClientWithDelegation(
	t *testing.T,
	store *Store,
	inventory *authorityInventory,
	reviews tokenReviewStub,
	hub *WatchHub,
	actor, effective auth.Account,
	effectiveUID string,
	delegatedMethods []string,
) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, authorityTestAudience, []string{actor.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	reader := auth.ServiceAccountUIDReaderFunc(func(_ context.Context, account auth.Account) (string, bool, error) {
		if account != effective {
			return "", false, nil
		}
		return effectiveUID, true, nil
	})
	methods, err := MethodsWithDelegation(resolver, auth.Delegator{
		Account: actor, CallerNamespaces: []string{effective.Namespace},
	}, reader, delegatedMethods)
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Store: store, DispatcherCallers: map[auth.Account]struct{}{actor: {}},
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	connection := grpcmethods.Serve(t, methods, func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	})
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

func TestRPCExecutionPrincipalAndPodBoundBackendAuthority(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 10)
	owner := serviceAccountIdentityFor(Account(caller), "owner-uid")
	inventory := &authorityInventory{
		sandbox: SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		account: owner, accountFound: true,
	}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"owner-account":   {account: caller, uid: owner.GetUid()},
		"owner-pod":       {account: caller, uid: owner.GetUid(), pod: true},
		"owner-replaced":  {account: caller, uid: "replacement-uid"},
		"foreign-account": {account: auth.Account{Namespace: caller.Namespace, Name: "test-stranger"}, uid: "foreign-uid"},
	}, hub)

	sessionID := uuid.NewString()
	opened, err := client.OpenSession(authorityContext(t, "owner-account"), &dispatcherpb.OpenSessionRequest{
		SessionId: sessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Spec: &sessionpb.SessionSpec{},
	})
	if err != nil {
		t.Fatalf("owner OpenSession: %v", err)
	}
	if opened.GetOpening().GetSessionId() != sessionID {
		t.Fatalf("OpenSession returned %#v", opened.GetOpening())
	}
	if _, err := client.SubmitInput(authorityContext(t, "owner-account"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-owned-input", SessionId: sessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "owned",
	}); err != nil {
		t.Fatalf("same-owner SubmitInput: %v", err)
	}
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID}); err != nil {
		t.Fatalf("same-owner GetSessionOpening: %v", err)
	}
	if _, err := client.GetDispatch(authorityContext(t, "owner-account"), &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); err != nil {
		t.Fatalf("same-owner GetDispatch: %v", err)
	}

	foreign := authorityContext(t, "foreign-account")
	if _, err := client.GetSessionOpening(foreign, &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign GetSessionOpening = %v, want NotFound", err)
	}
	if _, err := client.GetDispatch(foreign, &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign GetDispatch = %v, want NotFound", err)
	}
	if _, err := client.SubmitInput(foreign, &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-foreign-input", SessionId: sessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "foreign",
	}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("foreign SubmitInput = %v, want PermissionDenied", err)
	}

	legacy, err := store.OpenSession(ctx, "authority-legacy-session", caller.Name, authoritySandboxUID, &sessionpb.SessionSpec{}, nil, nil)
	if err != nil || legacy.ExecutionPrincipal != nil {
		t.Fatalf("seed legacy opening: %#v %v", legacy, err)
	}
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionOpeningRequest{SessionId: legacy.SessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("normal account read of ownerless legacy Session = %v, want NotFound", err)
	}
	if _, err := client.SubmitInput(authorityContext(t, "owner-account"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-legacy-normal-input", SessionId: legacy.SessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "must remain unowned",
	}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("normal write to ownerless legacy Session = %v, want PermissionDenied", err)
	}
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-pod"), &dispatcherpb.GetSessionOpeningRequest{SessionId: legacy.SessionID}); err != nil {
		t.Fatalf("configured Pod-bound backend read of legacy Session: %v", err)
	}
	if _, err := client.SubmitInput(authorityContext(t, "owner-pod"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-legacy-backend-input", SessionId: legacy.SessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "backend",
	}); err != nil {
		t.Fatalf("configured Pod-bound backend write of legacy Session: %v", err)
	}

	listed, err := client.ListDispatches(authorityContext(t, "owner-account"), &dispatcherpb.ListDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: legacy.SessionID},
	}})
	if err != nil || len(listed.GetDispatches()) != 0 {
		t.Fatalf("account-only caller listed legacy backend Dispatches: %#v %v", listed.GetDispatches(), err)
	}

	// An account-only token for the configured backend name has no Pod proof and cannot use its
	// privileged legacy path. The actual Pod-bound token can cancel without a live inventory read.
	inventory.failSandboxRead(errors.New("inventory unavailable"))
	if _, err := client.CancelDispatch(authorityContext(t, "owner-pod"), &dispatcherpb.CancelDispatchRequest{CommandId: "authority-legacy-backend-input"}); err != nil {
		t.Fatalf("privileged CancelDispatch depended on Sandbox inventory: %v", err)
	}
	inventory.failSandboxRead(nil)

	// The retained owner UID is immutable: a replacement ServiceAccount with the same name can
	// neither read the existing Session nor act as its old principal.
	replacement := authorityContext(t, "owner-replaced")
	if _, err := client.GetSessionOpening(replacement, &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("same-name replacement GetSessionOpening = %v, want NotFound", err)
	}
	inventory.setAccount(serviceAccountIdentityFor(Account(caller), "replacement-uid"))
	inventory.failSandboxRead(errors.New("inventory unavailable for retained reads"))
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID}); err != nil {
		t.Fatalf("retained read required a live Sandbox after account replacement: %v", err)
	}
	if _, err := client.GetDispatch(authorityContext(t, "owner-account"), &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); err != nil {
		t.Fatalf("retained Dispatch read required a live Sandbox: %v", err)
	}
	retained, err := client.ListDispatches(authorityContext(t, "owner-account"), &dispatcherpb.ListDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: sessionID},
	}})
	if err != nil || len(retained.GetDispatches()) != 1 || retained.GetDispatches()[0].GetCommandId() != "authority-owned-input" {
		t.Fatalf("retained list read required a live Sandbox or lost owner row: %#v %v", retained.GetDispatches(), err)
	}
	inventory.failSandboxRead(nil)
	if _, err := client.SubmitInput(authorityContext(t, "owner-account"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-revoked-input", SessionId: sessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "revoked",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("old principal write after ServiceAccount replacement = %v, want FailedPrecondition", err)
	}
	if _, err := client.SubmitInput(replacement, &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-replacement-input", SessionId: sessionID, Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "replacement",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("replacement principal write into old Session = %v, want FailedPrecondition", err)
	}

	watchCtx, stopWatch := context.WithCancel(authorityContext(t, "owner-account"))
	defer stopWatch()
	stream, err := client.WatchDispatches(watchCtx, &dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "authority-terminal-watch"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || first.GetCaughtUp() == nil {
		t.Fatalf("WatchDispatches initial frame = %#v, %v; want caught-up", first, err)
	}
	hub.notifyTerminal(TerminalEvent{
		ID: "foreign-terminal", Owner: serviceAccountIdentityFor(Account(caller), "replacement-uid"),
		Dispatch: &dispatcherpb.Dispatch{CommandId: "foreign-terminal-command", SessionId: "authority-terminal-watch", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED},
	})
	hub.notifyTerminal(TerminalEvent{
		ID: "owned-terminal", Owner: owner,
		Dispatch: &dispatcherpb.Dispatch{CommandId: "owned-terminal-command", SessionId: "authority-terminal-watch", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED},
	})
	terminalFrame, err := stream.Recv()
	if err != nil || terminalFrame.GetDispatch().GetCommandId() != "owned-terminal-command" {
		t.Fatalf("WatchDispatches leaked or missed terminal owner event: %#v %v", terminalFrame, err)
	}
}

func TestDelegatedActionsUseEffectiveCallerWithoutLeakingActorAuthority(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 10)
	effective := auth.Account{Namespace: caller.Namespace, Name: "delegated-effective-caller"}
	foreign := auth.Account{Namespace: caller.Namespace, Name: "delegated-foreign-caller"}
	const effectiveUID = "effective-caller-uid"
	const actorUID = "actions-actor-uid"
	snapshot := SandboxSnapshot{Name: effective.Name, Namespace: effective.Namespace, UID: authoritySandboxUID}
	inventory := &authorityInventory{
		sandbox: snapshot,
		account: serviceAccountIdentityFor(Account(effective), effectiveUID), accountFound: true,
	}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	delegatedMethods := []string{
		dispatcherpb.SessionDispatcher_OpenSession_FullMethodName,
		dispatcherpb.SessionDispatcher_SubmitInput_FullMethodName,
		dispatcherpb.SessionDispatcher_GetSessionOpening_FullMethodName,
		dispatcherpb.SessionDispatcher_GetDispatch_FullMethodName,
		dispatcherpb.SessionDispatcher_ListDispatches_FullMethodName,
		dispatcherpb.SessionDispatcher_CancelDispatch_FullMethodName,
		dispatcherpb.SessionDispatcher_InterruptTurn_FullMethodName,
		dispatcherpb.SessionDispatcher_WatchDispatches_FullMethodName,
	}
	client := authorityClientWithDelegation(t, store, inventory, tokenReviewStub{
		"actions-token":   {account: caller, uid: actorUID, pod: true},
		"effective-token": {account: effective, uid: effectiveUID},
	}, hub, caller, effective, effectiveUID, delegatedMethods)
	requestID, executionID := uuid.NewString(), uuid.NewString()
	delegated := func() context.Context {
		return delegatedAuthorityContext(t, "actions-token", effective, effectiveUID, requestID, executionID)
	}

	sessionID := uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	directOpening, err := client.OpenSession(authorityContext(t, "effective-token"), &dispatcherpb.OpenSessionRequest{
		SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Spec: spec,
	})
	if err != nil {
		t.Fatalf("direct effective caller open: %v", err)
	}
	inventory.failSandboxRead(errors.New("inventory unavailable after accepted opening"))
	openingRetry, err := client.OpenSession(delegated(), &dispatcherpb.OpenSessionRequest{
		SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Spec: spec,
	})
	if err != nil || openingRetry.GetOpening().GetSessionId() != sessionID {
		t.Fatalf("delegated idempotent opening retry: %#v %v", openingRetry, err)
	}
	inventory.failSandboxRead(nil)
	opening, err := store.GetOpening(ctx, sessionID)
	if err != nil || opening.Creator == nil || accountOfIdentity(opening.Creator) != Account(effective) || opening.Attribution == nil ||
		accountOfIdentity(opening.Attribution.Actor) != Account(effective) || accountOfIdentity(opening.Attribution.EffectiveCaller) != Account(effective) || opening.Attribution.Action != nil {
		t.Fatalf("delegated retry replaced direct opening admission evidence: %#v %v", opening, err)
	}
	if directOpening.GetOpening().GetSessionId() != sessionID || opening.WakeEligible {
		t.Fatalf("privileged actor's wake allowlist leaked to effective caller: response=%+v durable=%+v", directOpening.GetOpening(), opening)
	}

	first, err := client.SubmitInput(authorityContext(t, "effective-token"), &dispatcherpb.SubmitInputRequest{
		CommandId: "direct-to-action-retry", SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Text: "same domain command",
	})
	if err != nil {
		t.Fatalf("direct effective caller command: %v", err)
	}
	firstStored, err := store.GetDispatch(ctx, "direct-to-action-retry")
	if err != nil {
		t.Fatal(err)
	}
	second, err := client.SubmitInput(delegated(), &dispatcherpb.SubmitInputRequest{
		CommandId: "direct-to-action-retry", SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Text: "same domain command",
	})
	if err != nil || second.GetDispatch().GetCommandId() != first.GetDispatch().GetCommandId() {
		t.Fatalf("direct-to-delegated operation retry was not reconciled: first=%+v second=%+v err=%v", first, second, err)
	}
	directRetry, err := store.GetDispatch(ctx, "direct-to-action-retry")
	if err != nil || directRetry.Position != firstStored.Position || directRetry.Attribution == nil || accountOfIdentity(directRetry.Attribution.Actor) != Account(effective) ||
		accountOfIdentity(directRetry.Attribution.EffectiveCaller) != Account(effective) || directRetry.Attribution.Action != nil {
		t.Fatalf("Action retry replaced original direct admission evidence: %+v %v", directRetry, err)
	}

	created, err := client.SubmitInput(delegated(), &dispatcherpb.SubmitInputRequest{
		CommandId: "delegated-new-command", SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Text: "Action command",
	})
	if err != nil {
		t.Fatalf("delegated effective caller command: %v", err)
	}
	stored, err := store.GetDispatch(ctx, created.GetDispatch().GetCommandId())
	if err != nil || stored.Attribution == nil || accountOfIdentity(stored.Attribution.Actor) != Account(caller) ||
		stored.Attribution.Actor.GetUid() != actorUID || stored.Attribution.ActorPod == nil ||
		accountOfIdentity(stored.Attribution.EffectiveCaller) != Account(effective) || stored.Attribution.EffectiveCaller.GetUid() != effectiveUID ||
		stored.Attribution.Action == nil || stored.Attribution.Action.RequestID != requestID || stored.Attribution.Action.ExecutionID != executionID {
		t.Fatalf("delegated admission evidence was not durably retained distinctly: %+v %v", stored, err)
	}
	if stored.WakeEligible || stored.WakeCaller != nil {
		t.Fatalf("actor's wake allowlist made effective caller's command wake-eligible: %+v", stored)
	}
	if _, err := client.GetSessionOpening(delegated(), &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID}); err != nil {
		t.Fatalf("delegated caller could not read its own opening: %v", err)
	}
	if _, err := client.GetDispatch(delegated(), &dispatcherpb.GetDispatchRequest{CommandId: stored.CommandID}); err != nil {
		t.Fatalf("delegated caller could not read its own dispatch: %v", err)
	}

	// The Action actor can use its Pod-bound backend admission to access legacy rows with no
	// retained owner. That authority must not pass through to the effective caller.
	actorLegacy, err := store.OpenSession(ctx, "delegated-actor-legacy-session", snapshot.Name, snapshot.UID,
		&sessionpb.SessionSpec{}, nil, nil)
	if err != nil {
		t.Fatal("seed actor-only legacy opening:", err)
	}
	actorLegacyDispatch, err := store.Enqueue(ctx, actorLegacy.SessionID, snapshot.Name, snapshot.UID,
		input("delegated-actor-legacy-command", "actor-only legacy command"), nil)
	if err != nil {
		t.Fatal("seed actor-only legacy dispatch:", err)
	}
	if _, err := client.GetSessionOpening(authorityContext(t, "actions-token"), &dispatcherpb.GetSessionOpeningRequest{SessionId: actorLegacy.SessionID}); err != nil {
		t.Fatalf("Pod-bound actor could not read its backend-only legacy opening: %v", err)
	}
	if _, err := client.GetDispatch(authorityContext(t, "actions-token"), &dispatcherpb.GetDispatchRequest{CommandId: actorLegacyDispatch.CommandID}); err != nil {
		t.Fatalf("Pod-bound actor could not read its backend-only legacy dispatch: %v", err)
	}
	if _, err := client.GetSessionOpening(delegated(), &dispatcherpb.GetSessionOpeningRequest{SessionId: actorLegacy.SessionID}); status.Code(err) != codes.NotFound {
		t.Errorf("delegated effective caller inherited actor's backend opening access: %v", err)
	}
	if _, err := client.GetDispatch(delegated(), &dispatcherpb.GetDispatchRequest{CommandId: actorLegacyDispatch.CommandID}); status.Code(err) != codes.NotFound {
		t.Errorf("delegated effective caller inherited actor's backend dispatch access: %v", err)
	}
	if _, err := client.CancelDispatch(delegated(), &dispatcherpb.CancelDispatchRequest{CommandId: actorLegacyDispatch.CommandID}); status.Code(err) != codes.NotFound {
		t.Errorf("delegated effective caller inherited actor's backend cancellation authority: %v", err)
	}
	actorStillQueued, err := store.GetDispatch(ctx, actorLegacyDispatch.CommandID)
	if err != nil || actorStillQueued.State != QueueState {
		t.Errorf("denied delegated cancellation changed actor-only operation: %+v %v", actorStillQueued, err)
	}

	foreignID := "delegated-foreign-session"
	foreignIdentity := serviceAccountIdentityFor(Account(foreign), "foreign-caller-uid")
	foreignOpening, err := store.OpenSessionForCaller(ctx, foreignID, snapshot.Name, snapshot.UID,
		&sessionpb.SessionSpec{}, nil, nil, foreignIdentity, CallerIdentity{ServiceAccountIdentity: foreignIdentity, Attribution: &AdmissionAttribution{Actor: foreignIdentity, EffectiveCaller: foreignIdentity}})
	if err != nil {
		t.Fatal(err)
	}
	foreignDispatch, err := store.EnqueueForCaller(ctx, foreignID, snapshot.Name, snapshot.UID,
		input("delegated-foreign-command", "foreign"), nil, CallerIdentity{
			ServiceAccountIdentity: foreignIdentity,
			Attribution:            &AdmissionAttribution{Actor: foreignIdentity, EffectiveCaller: foreignIdentity},
		})
	if err != nil {
		t.Fatalf("seed foreign dispatch: %v", err)
	}
	if _, err := client.GetSessionOpening(delegated(), &dispatcherpb.GetSessionOpeningRequest{SessionId: foreignOpening.SessionID}); status.Code(err) != codes.NotFound {
		t.Errorf("privileged actor's backend membership leaked opening read: %v", err)
	}
	if _, err := client.GetDispatch(delegated(), &dispatcherpb.GetDispatchRequest{CommandId: foreignDispatch.CommandID}); status.Code(err) != codes.NotFound {
		t.Errorf("privileged actor's backend membership leaked dispatch lookup: %v", err)
	}
	listed, err := client.ListDispatches(delegated(), &dispatcherpb.ListDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_Sandbox{Sandbox: snapshot.Name},
	}})
	if err != nil {
		t.Fatalf("delegated sandbox dispatch list: %v", err)
	}
	for _, row := range listed.GetDispatches() {
		if row.GetCommandId() == foreignDispatch.CommandID || row.GetCommandId() == actorLegacyDispatch.CommandID {
			t.Fatalf("delegated sandbox list leaked a foreign or actor-only Session dispatch: %+v", row)
		}
	}
	if _, err := client.CancelDispatch(delegated(), &dispatcherpb.CancelDispatchRequest{CommandId: foreignDispatch.CommandID}); status.Code(err) != codes.NotFound {
		t.Errorf("privileged actor's backend membership leaked cancellation: %v", err)
	}
	stillQueued, err := store.GetDispatch(ctx, foreignDispatch.CommandID)
	if err != nil || stillQueued.State != QueueState {
		t.Errorf("denied delegated cancellation changed the foreign operation: %+v %v", stillQueued, err)
	}
	if _, err := client.InterruptTurn(delegated(), &dispatcherpb.InterruptTurnRequest{
		CommandId: "delegated-foreign-interrupt", SessionId: foreignID, Sandbox: snapshot.Name,
		SandboxUid: snapshot.UID, TurnId: "foreign-turn",
	}); status.Code(err) != codes.PermissionDenied {
		t.Errorf("privileged actor's backend membership leaked interrupt authority: %v", err)
	}
	if _, err := client.ChangeModel(delegated(), &dispatcherpb.ChangeModelRequest{
		CommandId: "delegated-unconfigured-method", SessionId: sessionID, Sandbox: snapshot.Name,
		SandboxUid: snapshot.UID, Model: "test-model",
	}); status.Code(err) != codes.Unauthenticated {
		t.Errorf("delegation escaped its exact RPC method list: %v", err)
	}

	watchCtx, cancelWatch := context.WithCancel(delegated())
	defer cancelWatch()
	watch, err := client.WatchDispatches(watchCtx, &dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_Sandbox{Sandbox: snapshot.Name},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var watched []string
	for {
		frame, err := watch.Recv()
		if err != nil {
			t.Fatalf("delegated dispatch watch: %v", err)
		}
		if dispatch := frame.GetDispatch(); dispatch != nil {
			watched = append(watched, dispatch.GetCommandId())
		}
		if frame.GetCaughtUp() != nil {
			break
		}
	}
	if slices.Contains(watched, foreignDispatch.CommandID) || slices.Contains(watched, actorLegacyDispatch.CommandID) ||
		!slices.Contains(watched, stored.CommandID) {
		t.Fatalf("delegated watch did not apply the same owner scope: %v", watched)
	}
	if _, err := client.CancelDispatch(delegated(), &dispatcherpb.CancelDispatchRequest{CommandId: stored.CommandID}); err != nil {
		t.Fatalf("delegated effective caller could not cancel its own queued command: %v", err)
	}
	if _, err := client.CancelDispatch(authorityContext(t, "actions-token"), &dispatcherpb.CancelDispatchRequest{CommandId: actorLegacyDispatch.CommandID}); err != nil {
		t.Fatalf("Pod-bound actor could not cancel its backend-only legacy dispatch: %v", err)
	}
}

type changingExecutionCatalog struct {
	testCatalog
	accounts []*ServiceAccountIdentity
	calls    int
}

func (c *changingExecutionCatalog) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	if c.calls >= len(c.accounts) {
		return nil, false, fmt.Errorf("unexpected extra execution account read %d", c.calls+1)
	}
	account := c.accounts[c.calls]
	c.calls++
	return account, true, nil
}

func TestPendingExecutionUIDBindsOnceAndReplacementStopsRunnerDelivery(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: pendingSandboxUID}
	pending := serviceAccountIdentityFor(Account(caller), "")
	opening, err := store.OpenSessionForCaller(
		ctx,
		"authority-pending-session",
		snapshot.Name,
		snapshot.UID,
		&sessionpb.SessionSpec{},
		nil,
		nil,
		pending,
		CallerIdentity{Privileged: true, Attribution: &AdmissionAttribution{Actor: serviceAccountIdentityFor(Account(caller), "creator-uid"), EffectiveCaller: serviceAccountIdentityFor(Account(caller), "creator-uid")}},
	)
	if err != nil || opening.ExecutionPrincipal == nil || opening.ExecutionPrincipal.GetUid() != "" {
		t.Fatalf("seed pending opening: %#v %v", opening, err)
	}
	catalog := &changingExecutionCatalog{
		testCatalog: testCatalog{sandbox: snapshot},
		accounts: []*ServiceAccountIdentity{
			serviceAccountIdentityFor(Account(caller), "managed-account-uid"),
			serviceAccountIdentityFor(Account(caller), "replacement-account-uid"),
		},
	}
	runner := &acceptingRunner{calls: make(chan string, 2)}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: testLease{store: store}, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	owner, err := worker.Leases.Acquire(ctx, snapshot.Name, snapshot.UID)
	if err != nil || owner == nil {
		t.Fatalf("acquire delivery owner: %#v %v", owner, err)
	}
	worker.owners[snapshot.UID] = *owner
	if err := worker.processSession(ctx, opening.SessionID); err != nil {
		t.Fatalf("process pending opening: %v", err)
	}
	if catalog.calls != 2 {
		t.Fatalf("worker read execution identity %d times, want bind then pre-delivery recheck", catalog.calls)
	}
	stored, err := store.GetOpening(ctx, opening.SessionID)
	if err != nil || stored.ExecutionPrincipal == nil || stored.ExecutionPrincipal.GetUid() != "managed-account-uid" {
		t.Fatalf("opening did not retain its first bound UID: %#v %v", stored, err)
	}
	if stored.State != FailedState || stored.Reason != "execution_identity_changed" {
		t.Fatalf("replacement identity did not fail queued opening: state=%q reason=%q", stored.State, stored.Reason)
	}
	select {
	case call := <-runner.calls:
		t.Fatalf("Runner received work after account replacement: %q", call)
	default:
	}
	if _, err := store.BindExecutionPrincipal(ctx, opening.SessionID, snapshot.UID, caller.Namespace, caller.Name, "managed-account-uid"); err != nil {
		t.Fatalf("idempotent UID bind: %v", err)
	}
	if _, err := store.BindExecutionPrincipal(ctx, opening.SessionID, snapshot.UID, caller.Namespace, caller.Name, "replacement-account-uid"); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("conflicting UID bind = %v, want ErrNotAuthorized", err)
	}
}

func TestOpeningRetryReadsDurableReceiptWithoutOwnerInventory(t *testing.T) {
	store := testStore(t, 3)
	owner := serviceAccountIdentityFor(caller, "durable-retry-owner-uid")
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: uuid.NewString()}
	inventory := &authorityInventory{sandbox: snapshot, account: owner, accountFound: true}
	foreign := Account{Namespace: caller.Namespace, Name: "retry-foreign"}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"retry-owner":    {account: caller, uid: owner.GetUid()},
		"retry-replaced": {account: caller, uid: "replacement-uid"},
		"retry-foreign":  {account: foreign, uid: "foreign-uid"},
		"retry-backend":  {account: caller, uid: "backend-uid", pod: true},
	}, nil)
	setup := "echo retained"
	request := &dispatcherpb.OpenSessionRequest{
		SessionId: uuid.NewString(), Sandbox: snapshot.Name,
		SandboxUid: snapshot.UID, Spec: &sessionpb.SessionSpec{}, SetupScript: &setup,
	}
	first, err := client.OpenSession(authorityContext(t, "retry-owner"), request)
	if err != nil {
		t.Fatal(err)
	}
	stored, err := store.GetOpening(t.Context(), request.GetSessionId())
	if err != nil {
		t.Fatal(err)
	}
	// Privileged openings may still be waiting for their execution UID to bind.
	inventory.mu.Lock()
	inventory.account, inventory.accountFound = nil, false
	inventory.mu.Unlock()
	pendingRequest := proto.Clone(request).(*dispatcherpb.OpenSessionRequest)
	pendingRequest.SessionId = uuid.NewString()
	pending, err := client.OpenSession(authorityContext(t, "retry-backend"), pendingRequest)
	if err != nil {
		t.Fatal(err)
	}
	inventory.failSandboxRead(errors.New("inventory unavailable"))
	pendingRetry, err := client.OpenSession(authorityContext(t, "retry-backend"), pendingRequest)
	if err != nil || !proto.Equal(pending, pendingRetry) {
		t.Fatalf("pending privileged retry: %v %v", pendingRetry, err)
	}
	if _, err := client.OpenSession(authorityContext(t, "retry-owner"), pendingRequest); status.Code(err) != codes.NotFound {
		t.Fatalf("unbound receipt granted normal authority: %v", err)
	}
	for _, token := range []string{"retry-owner", "retry-backend"} {
		again, err := client.OpenSession(authorityContext(t, token), request)
		if err != nil || !proto.Equal(first, again) {
			t.Fatalf("%s retry: %v %v", token, again, err)
		}
	}
	for _, change := range []struct {
		name  string
		apply func(*dispatcherpb.OpenSessionRequest)
	}{
		{"environment name", func(r *dispatcherpb.OpenSessionRequest) { r.Sandbox += "-other" }},
		{"environment UID", func(r *dispatcherpb.OpenSessionRequest) { r.SandboxUid = uuid.NewString() }},
		{"spec", func(r *dispatcherpb.OpenSessionRequest) { r.Spec.Instructions = "different instructions" }},
		{"absent spec", func(r *dispatcherpb.OpenSessionRequest) { r.Spec = nil }},
		{"setup", func(r *dispatcherpb.OpenSessionRequest) { r.SetupScript = nil }},
	} {
		conflict := proto.Clone(request).(*dispatcherpb.OpenSessionRequest)
		change.apply(conflict)
		if _, err := client.OpenSession(authorityContext(t, "retry-owner"), conflict); status.Code(err) != codes.AlreadyExists {
			t.Fatalf("%s conflict = %v", change.name, err)
		}
		for _, token := range []string{"retry-replaced", "retry-foreign"} {
			for _, r := range []*dispatcherpb.OpenSessionRequest{request, conflict} {
				if _, err := client.OpenSession(authorityContext(t, token), r); status.Code(err) != codes.NotFound {
					t.Fatalf("%s saw %s request details: %v", token, change.name, err)
				}
			}
		}
	}
	// Reading failed or uncertain materialization never requeues it or changes its evidence.
	for _, state := range []string{FailedState, DeliveringState} {
		if _, err := store.DB.ExecContext(t.Context(), "UPDATE session_opening SET state=$2,reason='retained outcome' WHERE session_id=$1", stored.SessionID, state); err != nil {
			t.Fatal(err)
		}
		if _, err := client.OpenSession(authorityContext(t, "retry-owner"), request); err != nil {
			t.Fatal(err)
		}
		after, err := store.GetOpening(t.Context(), stored.SessionID)
		if err != nil || after.State != state || after.Reason != "retained outcome" || !after.UpdatedAt.Equal(stored.UpdatedAt) || after.Creator.GetUid() != stored.Creator.GetUid() {
			t.Fatalf("receipt retry changed durable state: %+v %v", after, err)
		}
	}
	if _, err := store.EndSandbox(t.Context(), snapshot.UID); err != nil {
		t.Fatal(err)
	}
	if _, err := client.OpenSession(authorityContext(t, "retry-owner"), request); status.Code(err) != codes.Unavailable {
		t.Fatalf("absent receipt and unavailable inventory: %v", err)
	}
	inventory.failSandboxRead(nil)
	inventory.mu.Lock()
	inventory.sandbox = SandboxSnapshot{}
	inventory.mu.Unlock()
	if _, err := client.OpenSession(authorityContext(t, "retry-owner"), request); status.Code(err) != codes.NotFound {
		t.Fatalf("absent receipt and deleted environment: %v", err)
	}
	// With the merged deletion fence, even a stale live inventory cannot turn a
	// purged receipt retry into a new admission for the deleted environment UID.
	inventory.mu.Lock()
	inventory.sandbox, inventory.account, inventory.accountFound = snapshot, owner, true
	inventory.mu.Unlock()
	if _, err := client.OpenSession(authorityContext(t, "retry-owner"), request); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("purged receipt with stale live inventory crossed deletion fence: %v", err)
	}
	if _, err := store.GetOpening(t.Context(), stored.SessionID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("retry recreated cleaned-up opening: %v", err)
	}
}

func TestOpeningTransactionChecksRetainedAuthorityBeforeConflict(t *testing.T) {
	store := testStore(t, 3)
	owner := serviceAccountIdentityFor(caller, "transaction-owner-uid")
	foreign := serviceAccountIdentityFor(Account{Namespace: caller.Namespace, Name: "transaction-foreign"}, "foreign-uid")
	ownerCaller := CallerIdentity{ServiceAccountIdentity: owner, Attribution: &AdmissionAttribution{Actor: owner, EffectiveCaller: owner}}
	foreignCaller := CallerIdentity{ServiceAccountIdentity: foreign, Attribution: &AdmissionAttribution{Actor: foreign, EffectiveCaller: foreign}}
	id, uid := uuid.NewString(), uuid.NewString()
	if _, err := store.OpenSessionForCaller(t.Context(), id, "transaction-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil, owner, ownerCaller); err != nil {
		t.Fatal(err)
	}
	// This is the transactional path after another opener wins the Session-ID lock,
	// including when the API's initial read did not yet see that winning opening.
	for _, name := range []string{"transaction-sandbox", "conflicting-sandbox"} {
		if _, err := store.OpenSessionForCaller(t.Context(), id, name, uid, &sessionpb.SessionSpec{}, nil, nil, foreign, foreignCaller); !errors.Is(err, ErrNotFound) {
			t.Fatalf("transaction exposed an existing opening to foreign caller: %v", err)
		}
	}
}

func TestOpeningDiscoveryUsesRetainedAuthorityAndStablePages(t *testing.T) {
	store := testStore(t, 3)
	owner := serviceAccountIdentityFor(caller, "discovery-owner-uid")
	foreignAccount := Account{Namespace: caller.Namespace, Name: "discovery-foreign"}
	foreign := serviceAccountIdentityFor(foreignAccount, "discovery-foreign-uid")
	uid := uuid.NewString()
	const queuedID = "00000000-0000-4000-8000-000000000001"
	const foreignID = "00000000-0000-4000-8000-000000000002"
	const failedID = "00000000-0000-4000-8000-000000000003"
	const pendingID = "00000000-0000-4000-8000-000000000004"
	// Insert out of order; ordering is the immutable public ID, never delivery state.
	for _, row := range []struct {
		id        string
		principal *ServiceAccountIdentity
	}{
		{failedID, owner},
		{foreignID, foreign},
		{queuedID, owner},
		{pendingID, serviceAccountIdentityFor(caller, "")},
	} {
		if _, err := store.OpenSessionForCaller(t.Context(), row.id, "discovery-sandbox", uid,
			&sessionpb.SessionSpec{}, nil, nil, row.principal, CallerIdentity{Privileged: true, ServiceAccountIdentity: owner, Attribution: &AdmissionAttribution{Actor: owner, EffectiveCaller: owner}}); err != nil {
			t.Fatal(err)
		}
	}
	lease, err := store.ClaimSandboxOwner(t.Context(), uid, "discovery-lease", 1, "discovery-worker", 0)
	if err != nil || lease == nil {
		t.Fatalf("claim: %+v %v", lease, err)
	}
	if row, err := store.SettleOpening(t.Context(), failedID, *lease, []string{QueueState}, FailedState, "runner_rejected"); err != nil || row == nil {
		t.Fatalf("reject opening: %+v %v", row, err)
	}
	inventory := &authorityInventory{sandboxErr: errors.New("owner environment unavailable")}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"discovery-owner":       {account: caller, uid: owner.GetUid()},
		"discovery-replacement": {account: caller, uid: "replacement-account-uid"},
		"discovery-foreign":     {account: foreignAccount, uid: foreign.GetUid()},
		"discovery-backend":     {account: caller, uid: "backend-account-uid", pod: true},
	}, nil)
	first, err := client.ListSessionOpenings(authorityContext(t, "discovery-owner"), &dispatcherpb.ListSessionOpeningsRequest{Limit: 1})
	if err != nil || len(first.GetOpenings()) != 1 || first.GetOpenings()[0].GetSessionId() != queuedID || first.GetNextPageToken() == "" {
		t.Fatalf("owner first page: %v %v", first, err)
	}
	if first.GetOpenings()[0].GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER || first.GetOpenings()[0].GetSandboxUid() != uid {
		t.Fatalf("queued opening placement/state: %v", first)
	}
	// The last row of a page can disappear between requests without invalidating its cursor.
	if _, err := store.DB.ExecContext(t.Context(), "DELETE FROM session_opening WHERE session_id=$1", queuedID); err != nil {
		t.Fatal(err)
	}
	second, err := client.ListSessionOpenings(authorityContext(t, "discovery-owner"), &dispatcherpb.ListSessionOpeningsRequest{
		Limit: 1, PageToken: first.GetNextPageToken(),
	})
	if err != nil || len(second.GetOpenings()) != 1 || second.GetOpenings()[0].GetSessionId() != failedID || second.GetNextPageToken() != "" ||
		second.GetOpenings()[0].GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED {
		t.Fatalf("owner second page: %v %v", second, err)
	}
	// A cursor carries position only; it never grants the original caller's authority.
	for _, test := range []struct {
		token string
		want  []string
	}{
		{"discovery-replacement", nil},
		{"discovery-foreign", []string{foreignID}},
		{"discovery-backend", []string{foreignID, failedID, pendingID}},
	} {
		page, err := client.ListSessionOpenings(authorityContext(t, test.token), &dispatcherpb.ListSessionOpeningsRequest{
			PageToken: first.GetNextPageToken(),
		})
		if err != nil {
			t.Fatal(err)
		}
		var ids []string
		for _, opening := range page.GetOpenings() {
			ids = append(ids, opening.GetSessionId())
		}
		if !slices.Equal(ids, test.want) || page.GetNextPageToken() != "" {
			t.Fatalf("%s read scope: %v, next %q; want %v", test.token, ids, page.GetNextPageToken(), test.want)
		}
	}
	for _, request := range []*dispatcherpb.ListSessionOpeningsRequest{
		{Limit: 101}, {PageToken: "not-a-page-token"}, {PageToken: string(make([]byte, 129))},
	} {
		if _, err := client.ListSessionOpenings(authorityContext(t, "discovery-owner"), request); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid page request %v: %v", request, err)
		}
	}
}
