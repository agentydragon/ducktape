package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const environmentGrantPolicyNamespace = "test-dispatcher-environment-grants"

func nativeCreateRequest(sessionID, sandboxName, sandboxUID string) *dispatcherpb.CreateSessionRequest {
	return &dispatcherpb.CreateSessionRequest{
		SessionId: sessionID,
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
				Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID},
				Harness:     sessionpb.Harness_HARNESS_CODEX,
			},
		}}},
	}
}

func addEnvironmentGrant(
	rules *reader.Index[access.Rule],
	name string,
	subject *policyapi.Identity,
	operation string,
	selector policyapi.EnvironmentSelector,
	expiresAt *metav1.Time,
) {
	rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: "environment", Operations: []string{operation}, Environments: []policyapi.EnvironmentSelector{selector},
		}}},
	})
	rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Identity: &policyapi.IdentityRef{Name: subject.Name}},
			PolicySets: []string{name}, ExpiresAt: expiresAt,
		},
	})
}

func acquireSandboxAdvisoryLock(t *testing.T, queueStore *store.Store, sandboxUID string) *sql.Conn {
	t.Helper()
	gate, err := queueStore.DB.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = gate.Close() })
	rollbackContext := context.WithoutCancel(t.Context())
	t.Cleanup(func() { _, _ = gate.ExecContext(rollbackContext, "ROLLBACK") })
	if _, err := gate.ExecContext(t.Context(), "BEGIN"); err != nil {
		t.Fatal(err)
	}
	if _, err := gate.ExecContext(t.Context(), "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "sandbox:"+sandboxUID); err != nil {
		t.Fatal(err)
	}
	return gate
}

func waitForCreateSessionBlockedBySandboxLock(t *testing.T, queueStore *store.Store, gate *sql.Conn) {
	t.Helper()
	var gatePID int
	if err := gate.QueryRowContext(t.Context(), "SELECT pg_backend_pid()").Scan(&gatePID); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		var blocked bool
		if err := queueStore.DB.QueryRowContext(t.Context(), `SELECT EXISTS (
			SELECT 1 FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)))`, gatePID).Scan(&blocked); err != nil {
			t.Fatal(err)
		}
		if blocked {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("CreateSession did not wait on the fixture's Sandbox lock")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestIdentityNativeCreateRequiresCurrentBindSessionGrant(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "native-bind-session-owner", environmentGrantPolicyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	sandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: sandbox, identity: ownerIdentity}
	rules := access.NewIndex()
	rules.PutIdentity(owner.Identity)
	var policyError error
	queueStore, dial := serveIdentitySVIDsWithPolicy(t, owner.Objects, environmentGrantPolicyNamespace, inventory, rules,
		func() error { return policyError })
	client := dial(owner.URI)
	request := func() *dispatcherpb.CreateSessionRequest {
		return nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)
	}

	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("missing bind_session grant on caller's own Sandbox = %v, want PermissionDenied", err)
	}
	addEnvironmentGrant(rules, "resume-only", owner.Identity, "resume", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	}, nil)
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("resume-only grant admitted native creation: %v", err)
	}
	rules.DeleteBinding("resume-only")
	rules.DeletePolicySet("resume-only")

	expired := metav1.NewTime(time.Now().Add(-time.Minute))
	addEnvironmentGrant(rules, "expired-bind-session", owner.Identity, "bind_session", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	}, &expired)
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("expired bind_session grant admitted native creation: %v", err)
	}
	rules.DeleteBinding("expired-bind-session")
	rules.DeletePolicySet("expired-bind-session")

	addEnvironmentGrant(rules, "live-bind-session", owner.Identity, "bind_session", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	}, nil)
	rules.PutIdentity(&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: owner.Identity.Name, UID: types.UID(uuid.NewString())}})
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("stale caller UID used an Identity grant: %v", err)
	}
	rules.PutIdentity(owner.Identity)
	policyError = errors.New("environment rule watch unavailable")
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.Unavailable {
		t.Fatalf("CreateSession with unavailable policy watch = %v, want Unavailable", err)
	}
	policyError = nil

	createdID := uuid.NewString()
	created, err := client.CreateSession(t.Context(), nativeCreateRequest(createdID, sandbox.Name, sandbox.UID))
	if err != nil || created.GetSession().GetIdentity().GetName() != ownerIdentity.GetName() || created.GetSession().GetIdentity().GetUid() != ownerIdentity.GetUid() {
		t.Fatalf("self bind_session native CreateSession = %v, %v", created, err)
	}
	stored, err := queueStore.GetSession(t.Context(), createdID)
	if err != nil || !proto.Equal(stored.ExecutionIdentity, ownerIdentity) {
		t.Fatalf("new Session execution Identity = %v, %v, want %v", stored, err, ownerIdentity)
	}
	creator, ok := stored.Attribution.(*store.IdentityAttribution)
	if !ok || !proto.Equal(creator.Identity, ownerIdentity) {
		t.Fatalf("direct creator provenance = %#v, want Identity %v", stored.Attribution, ownerIdentity)
	}

	central := &dispatcherpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{
			CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}},
		}}},
	}
	if _, err := client.CreateSession(t.Context(), central); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("Identity caller used central placement: %v, want PermissionDenied", err)
	}

	inventory.mu.Lock()
	inventory.identityMissing = true
	inventory.environment = &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: types.UID(sandbox.UID),
	}}
	inventory.environment.Spec.Running = true
	inventory.mu.Unlock()
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.NotFound {
		t.Fatalf("Identity caller created on an unbound Sandbox: %v, want NotFound", err)
	}

	inventory.mu.Lock()
	inventory.identityMissing = false
	inventory.identity = ownerIdentity
	inventory.environment = &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: types.UID(sandbox.UID),
	}}
	inventory.environment.Spec.Identity = &sandboxapi.IdentityRef{Name: ownerIdentity.GetName()}
	inventory.environment.Spec.Running = false
	inventory.mu.Unlock()
	if _, err := client.CreateSession(t.Context(), request()); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Identity caller created on a stopped Sandbox: %v, want FailedPrecondition", err)
	}
}

func TestIdentityNativeCreateRechecksTargetAfterSandboxLock(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "native-bind-session-lock-owner", environmentGrantPolicyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	sandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: sandbox, identity: ownerIdentity}
	rules := access.NewIndex()
	rules.PutIdentity(owner.Identity)
	addEnvironmentGrant(rules, "lock-bind-session", owner.Identity, "bind_session", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Name: ownerIdentity.GetName()},
	}, nil)
	queueStore, dial := serveIdentitySVIDsWithPolicy(t, owner.Objects, environmentGrantPolicyNamespace, inventory, rules, func() error { return nil })
	client := dial(owner.URI)
	request := nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)

	gate := acquireSandboxAdvisoryLock(t, queueStore, sandbox.UID)
	createDone := make(chan error, 1)
	go func() {
		_, err := client.CreateSession(t.Context(), request)
		createDone <- err
	}()
	// Observe the CreateSession transaction blocked by this fixture's lock before changing the target.
	waitForCreateSessionBlockedBySandboxLock(t, queueStore, gate)

	inventory.mu.Lock()
	inventory.identity = &identitypb.Identity{Name: "rebound-identity", Uid: uuid.NewString()}
	inventory.mu.Unlock()
	if _, err := gate.ExecContext(t.Context(), "COMMIT"); err != nil {
		t.Fatal(err)
	}
	if err := <-createDone; status.Code(err) != codes.NotFound {
		t.Fatalf("post-lock target Identity rebind = %v, want NotFound", err)
	}
	if _, err := queueStore.GetSession(t.Context(), request.GetSessionId()); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("post-lock rebind left a Session receipt: %v", err)
	}
}

func TestIdentityNativeCreateHidesSandboxDeletedWhileWaitingForLock(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "native-bind-session-deleted-owner", environmentGrantPolicyNamespace)
	identity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	sandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: sandbox, identity: identity}
	rules := access.NewIndex()
	rules.PutIdentity(owner.Identity)
	addEnvironmentGrant(rules, "deleted-bind-session", owner.Identity, "bind_session", policyapi.EnvironmentSelector{
		BoundIdentity: &policyapi.BoundIdentitySelector{Self: true},
	}, nil)
	queueStore, dial := serveIdentitySVIDsWithPolicy(t, owner.Objects, environmentGrantPolicyNamespace, inventory, rules, func() error { return nil })
	client := dial(owner.URI)
	request := nativeCreateRequest(uuid.NewString(), sandbox.Name, sandbox.UID)

	gate := acquireSandboxAdvisoryLock(t, queueStore, sandbox.UID)
	createDone := make(chan error, 1)
	go func() {
		_, err := client.CreateSession(t.Context(), request)
		createDone <- err
	}()
	// Observe the CreateSession transaction blocked by this fixture's lock before changing the target.
	waitForCreateSessionBlockedBySandboxLock(t, queueStore, gate)
	if _, err := gate.ExecContext(t.Context(), "INSERT INTO deleted_sandbox (sandbox_uid) VALUES ($1)", sandbox.UID); err != nil {
		t.Fatal(err)
	}
	if _, err := gate.ExecContext(t.Context(), "COMMIT"); err != nil {
		t.Fatal(err)
	}
	if err := <-createDone; status.Code(err) != codes.NotFound {
		t.Fatalf("Identity CreateSession after Sandbox deletion = %v, want NotFound", err)
	}
	if _, err := queueStore.GetSession(t.Context(), request.GetSessionId()); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("deleted Sandbox left a Session receipt: %v", err)
	}
}
