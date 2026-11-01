package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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
	account      ServiceAccountIdentity
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

func (i *authorityInventory) GetExecutionAccount(_ context.Context, sandbox SandboxSnapshot) (ServiceAccountIdentity, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if sandbox.Name != i.sandbox.Name || sandbox.UID != i.sandbox.UID {
		return ServiceAccountIdentity{}, false, nil
	}
	return i.account, i.accountFound, nil
}

func (i *authorityInventory) setAccount(account ServiceAccountIdentity) {
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
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, Uid: uid,
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
		WakeCallers: map[auth.Account]struct{}{actor: {}},
		Sandboxes:   inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	connection := grpcmethods.Serve(t, methods, func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	})
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

func TestRPCExecutionPrincipalAndPodBoundBackendAuthority(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 10)
	owner := ServiceAccountIdentity{Account: Account(caller), UID: "owner-uid"}
	inventory := &authorityInventory{
		sandbox: SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		account: owner, accountFound: true,
	}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"owner-account":   {account: caller, uid: owner.UID},
		"owner-pod":       {account: caller, uid: owner.UID, pod: true},
		"owner-replaced":  {account: caller, uid: "replacement-uid"},
		"foreign-account": {account: auth.Account{Namespace: caller.Namespace, Name: "test-stranger"}, uid: "foreign-uid"},
	}, hub)

	opened, err := client.OpenSession(authorityContext(t, "owner-account"), &dispatcherpb.OpenSessionRequest{
		SessionId: "authority-owned-session", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Spec: &sessionpb.SessionSpec{},
	})
	if err != nil {
		t.Fatalf("owner OpenSession: %v", err)
	}
	if opened.GetOpening().GetSessionId() != "authority-owned-session" {
		t.Fatalf("OpenSession returned %#v", opened.GetOpening())
	}
	if _, err := client.SubmitInput(authorityContext(t, "owner-account"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-owned-input", SessionId: "authority-owned-session", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "owned",
	}); err != nil {
		t.Fatalf("same-owner SubmitInput: %v", err)
	}
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionOpeningRequest{SessionId: "authority-owned-session"}); err != nil {
		t.Fatalf("same-owner GetSessionOpening: %v", err)
	}
	if _, err := client.GetDispatch(authorityContext(t, "owner-account"), &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); err != nil {
		t.Fatalf("same-owner GetDispatch: %v", err)
	}

	foreign := authorityContext(t, "foreign-account")
	if _, err := client.GetSessionOpening(foreign, &dispatcherpb.GetSessionOpeningRequest{SessionId: "authority-owned-session"}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign GetSessionOpening = %v, want NotFound", err)
	}
	if _, err := client.GetDispatch(foreign, &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign GetDispatch = %v, want NotFound", err)
	}
	if _, err := client.SubmitInput(foreign, &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-foreign-input", SessionId: "authority-owned-session", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "foreign",
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
	if _, err := client.GetSessionOpening(replacement, &dispatcherpb.GetSessionOpeningRequest{SessionId: "authority-owned-session"}); status.Code(err) != codes.NotFound {
		t.Fatalf("same-name replacement GetSessionOpening = %v, want NotFound", err)
	}
	inventory.setAccount(ServiceAccountIdentity{Account: Account(caller), UID: "replacement-uid"})
	inventory.failSandboxRead(errors.New("inventory unavailable for retained reads"))
	if _, err := client.GetSessionOpening(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionOpeningRequest{SessionId: "authority-owned-session"}); err != nil {
		t.Fatalf("retained read required a live Sandbox after account replacement: %v", err)
	}
	if _, err := client.GetDispatch(authorityContext(t, "owner-account"), &dispatcherpb.GetDispatchRequest{CommandId: "authority-owned-input"}); err != nil {
		t.Fatalf("retained Dispatch read required a live Sandbox: %v", err)
	}
	retained, err := client.ListDispatches(authorityContext(t, "owner-account"), &dispatcherpb.ListDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "authority-owned-session"},
	}})
	if err != nil || len(retained.GetDispatches()) != 1 || retained.GetDispatches()[0].GetCommandId() != "authority-owned-input" {
		t.Fatalf("retained list read required a live Sandbox or lost owner row: %#v %v", retained.GetDispatches(), err)
	}
	inventory.failSandboxRead(nil)
	if _, err := client.SubmitInput(authorityContext(t, "owner-account"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-revoked-input", SessionId: "authority-owned-session", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "revoked",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("old principal write after ServiceAccount replacement = %v, want FailedPrecondition", err)
	}
	if _, err := client.SubmitInput(replacement, &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-replacement-input", SessionId: "authority-owned-session", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, Text: "replacement",
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
		ID: "foreign-terminal", Owner: &ServiceAccountIdentity{Account: Account(caller), UID: "replacement-uid"},
		Dispatch: &dispatcherpb.Dispatch{CommandId: "foreign-terminal-command", SessionId: "authority-terminal-watch", Sandbox: caller.Name, SandboxUid: authoritySandboxUID, State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED},
	})
	hub.notifyTerminal(TerminalEvent{
		ID: "owned-terminal", Owner: &owner,
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
		account: ServiceAccountIdentity{Account: Account(effective), UID: effectiveUID}, accountFound: true,
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

	const sessionID = "delegated-owned-session"
	spec := &runnerpb.SessionSpec{}
	directOpening, err := client.OpenSession(authorityContext(t, "effective-token"), &dispatcherpb.OpenSessionRequest{
		SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Spec: spec,
	})
	if err != nil {
		t.Fatalf("direct effective caller open: %v", err)
	}
	openingRetry, err := client.OpenSession(delegated(), &dispatcherpb.OpenSessionRequest{
		SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Spec: spec,
	})
	if err != nil || openingRetry.GetOpening().GetSessionId() != sessionID {
		t.Fatalf("delegated idempotent opening retry: %#v %v", openingRetry, err)
	}
	opening, err := store.GetOpening(ctx, sessionID)
	if err != nil || opening.Creator == nil || opening.Creator.Account != Account(effective) || opening.Attribution == nil ||
		opening.Attribution.Actor.Account != Account(effective) || opening.Attribution.EffectiveCaller.Account != Account(effective) || opening.Attribution.Action != nil {
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
	if err != nil || directRetry.Position != firstStored.Position || directRetry.Attribution == nil || directRetry.Attribution.Actor.Account != Account(effective) ||
		directRetry.Attribution.EffectiveCaller.Account != Account(effective) || directRetry.Attribution.Action != nil {
		t.Fatalf("Action retry replaced original direct admission evidence: %+v %v", directRetry, err)
	}

	created, err := client.SubmitInput(delegated(), &dispatcherpb.SubmitInputRequest{
		CommandId: "delegated-new-command", SessionId: sessionID, Sandbox: snapshot.Name, SandboxUid: snapshot.UID, Text: "Action command",
	})
	if err != nil {
		t.Fatalf("delegated effective caller command: %v", err)
	}
	stored, err := store.GetDispatch(ctx, created.GetDispatch().GetCommandId())
	if err != nil || stored.Attribution == nil || stored.Attribution.Actor.Account != Account(caller) ||
		stored.Attribution.Actor.UID != actorUID || stored.Attribution.ActorPod == nil ||
		stored.Attribution.EffectiveCaller.Account != Account(effective) || stored.Attribution.EffectiveCaller.UID != effectiveUID ||
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
		&runnerpb.SessionSpec{}, nil, nil)
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
	foreignIdentity := ServiceAccountIdentity{Account: Account(foreign), UID: "foreign-caller-uid"}
	foreignOpening, err := store.OpenSessionWithIdentity(ctx, foreignID, snapshot.Name, snapshot.UID,
		&runnerpb.SessionSpec{}, nil, nil, &foreignIdentity, &foreignIdentity, nil)
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
	accounts []ServiceAccountIdentity
	calls    int
}

func (c *changingExecutionCatalog) GetExecutionAccount(context.Context, SandboxSnapshot) (ServiceAccountIdentity, bool, error) {
	if c.calls >= len(c.accounts) {
		return ServiceAccountIdentity{}, false, fmt.Errorf("unexpected extra execution account read %d", c.calls+1)
	}
	account := c.accounts[c.calls]
	c.calls++
	return account, true, nil
}

func TestPendingExecutionUIDBindsOnceAndReplacementStopsRunnerDelivery(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: pendingSandboxUID}
	pending := ServiceAccountIdentity{Account: Account(caller)}
	opening, err := store.OpenSessionWithIdentity(
		ctx,
		"authority-pending-session",
		snapshot.Name,
		snapshot.UID,
		&sessionpb.SessionSpec{},
		nil,
		nil,
		&pending,
		&ServiceAccountIdentity{Account: Account(caller), UID: "creator-uid"},
		nil,
	)
	if err != nil || opening.ExecutionPrincipal == nil || opening.ExecutionPrincipal.UID != "" {
		t.Fatalf("seed pending opening: %#v %v", opening, err)
	}
	catalog := &changingExecutionCatalog{
		testCatalog: testCatalog{sandbox: snapshot},
		accounts: []ServiceAccountIdentity{
			{Account: Account(caller), UID: "managed-account-uid"},
			{Account: Account(caller), UID: "replacement-account-uid"},
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
	if err != nil || stored.ExecutionPrincipal == nil || stored.ExecutionPrincipal.UID != "managed-account-uid" {
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
