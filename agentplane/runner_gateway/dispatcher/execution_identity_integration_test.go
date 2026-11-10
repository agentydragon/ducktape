package dispatcher

import (
	"context"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const identitySandboxUID = "00000000-0000-4000-8000-000000009021"

// identityCatalog is a live Sandbox view whose execution account and named Identity a test sets.
type identityCatalog struct {
	testCatalog
	account  *ServiceAccountIdentity
	identity *identitypb.Identity
}

func (c *identityCatalog) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return c.account, true, nil
}

func (c *identityCatalog) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return c.identity, true, nil
}

// placingRunner records who each placed Session executes as.
type placingRunner struct {
	acceptingRunner
	placed chan SessionExecution
}

func (r *placingRunner) CreateSession(_ context.Context, _ SandboxSnapshot, _ string, _ *sessionpb.SessionSpec, _ *string, execution SessionExecution) error {
	r.placed <- execution
	return nil
}

func TestSessionExecutesAsTheIdentityItsSandboxNamedAtCreation(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 10)
	owner := serviceAccountIdentityFor(Account(caller), "owner-uid")
	named := &identitypb.Identity{Name: "named-identity", Uid: "00000000-0000-4000-8000-0000000090a1"}
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: identitySandboxUID}
	inventory := &authorityInventory{sandbox: snapshot, account: owner, accountFound: true, identity: named}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	client := authorityClient(t, store, inventory, tokenReviewStub{"owner-account": {account: caller, uid: owner.GetUid()}}, hub)
	open := func() string {
		sessionID := uuid.NewString()
		if _, err := client.CreateSession(authorityContext(t, "owner-account"), &dispatcherpb.CreateSessionRequest{
			SessionId: sessionID, Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: snapshot.Name, Uid: snapshot.UID}, Harness: sessionpb.Harness_HARNESS_CODEX}}}},
		}); err != nil {
			t.Fatalf("CreateSession: %v", err)
		}
		return sessionID
	}
	placed, stranded := open(), open()
	stored, err := store.GetSession(ctx, placed)
	if err != nil || !proto.Equal(stored.ExecutionIdentity, named) || !proto.Equal(stored.ExecutionPrincipal, owner) {
		t.Fatalf("stored Session executes as %v / %v (%v), want %v beside %v", stored.ExecutionIdentity, stored.ExecutionPrincipal, err, named, owner)
	}
	read, err := client.GetSession(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionRequest{SessionId: placed})
	if err != nil || !proto.Equal(read.GetSession().GetIdentity(), named) {
		t.Fatalf("GetSession reads the Session as executing as %v (%v), want %v", read.GetSession().GetIdentity(), err, named)
	}

	catalog := &identityCatalog{testCatalog: testCatalog{sandbox: snapshot}, account: owner, identity: named}
	runner := &placingRunner{placed: make(chan SessionExecution, 2)}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: testLease{store: store}, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	lease, err := worker.Leases.Acquire(ctx, snapshot.Name, snapshot.UID)
	if err != nil || lease == nil {
		t.Fatalf("acquire delivery owner: %#v %v", lease, err)
	}
	worker.owners[snapshot.UID] = *lease
	if err := worker.processSession(ctx, placed); err != nil {
		t.Fatalf("place Session: %v", err)
	}
	select {
	case execution := <-runner.placed:
		if !proto.Equal(execution.Identity, named) || !proto.Equal(execution.Principal, owner) {
			t.Fatalf("placed Session executes as %v / %v, want %v beside %v", execution.Identity, execution.Principal, named, owner)
		}
	default:
		t.Fatal("Session was not placed")
	}

	// The Sandbox now names another Identity: the Session created under the first is not placed.
	catalog.identity = &identitypb.Identity{Name: "named-identity", Uid: "00000000-0000-4000-8000-0000000090a2"}
	if err := worker.processSession(ctx, stranded); err != nil {
		t.Fatalf("process stranded Session: %v", err)
	}
	refused, err := store.GetSession(ctx, stranded)
	if err != nil || refused.State != SessionFailedState || refused.Reason != "execution_identity_changed" {
		t.Fatalf("Session under a replaced Identity: %#v %v, want failed execution_identity_changed", refused, err)
	}
	select {
	case execution := <-runner.placed:
		t.Fatalf("Session under a replaced Identity was placed as %v", execution)
	default:
	}
}
