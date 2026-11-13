package orchestrationv1

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestALaunchCreatesItsSandboxAndGrantsTheCallerWhatItNames(t *testing.T) {
	f := newFixture(t)
	launched := resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, `{
		"name": "test-worker", "template": "test-template", "policy_sets": ["test-extra-policy"],
		"inbox_tags": ["test-tag"], "bootstrap": "true", "wake_on_send": true, "idle_suspend_after_seconds": 600,
		"launcher_grants": ["submit_input", "send_message", "create_session", "read_turns", "wait_for_turn"]
	}`))
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker"}, &sandbox); err != nil {
		t.Fatal(err)
	}
	if launched.Sandbox != (contract.SandboxRef{Name: "test-worker", UID: sandbox.UID}) {
		t.Errorf("launch answered %v, want the Sandbox %s", launched.Sandbox, sandbox.UID)
	}
	var intent struct {
		Caller struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		} `json:"caller"`
		Request struct {
			LaunchOrigin struct {
				Caller struct {
					Namespace string `json:"namespace"`
					Name      string `json:"name"`
				} `json:"caller"`
				ActionGroup string `json:"action_group"`
			} `json:"launch_origin"`
		} `json:"request"`
	}
	if err := json.Unmarshal([]byte(sandbox.Annotations[sandboxapi.CreateIntentAnnotation]), &intent); err != nil {
		t.Fatal(err)
	}
	if intent.Caller.Namespace != f.namespace || intent.Caller.Name != "actions" ||
		intent.Request.LaunchOrigin.Caller.Namespace != f.caller.Namespace ||
		intent.Request.LaunchOrigin.Caller.Name != f.caller.Name || intent.Request.LaunchOrigin.ActionGroup != testGroup {
		t.Errorf("launch attribution = %+v", intent)
	}
	if want := []string{"create_session", "read_turns", "send_message", "submit_input", "wait_for_turn"}; !slices.Equal(launched.LauncherGrants, want) {
		t.Errorf("launch granted %q, want %q", launched.LauncherGrants, want)
	}
	spec := sandbox.Spec
	if spec.Template != "test-template" || !spec.Running || !spec.WakeOnSend || spec.Bootstrap != "true" ||
		spec.IdleSuspendAfter == nil || spec.IdleSuspendAfter.Duration != 10*time.Minute ||
		spec.Identity == nil || spec.Identity.Name != "test-worker" || !slices.Equal(spec.InboxTags, []string{"test-tag"}) {
		t.Errorf("the Sandbox's spec is %+v", spec)
	}
	// The launch created the Identity the Sandbox acts as and granted it exactly the requested sets.
	var identity policyapi.Identity
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker"}, &identity); err != nil {
		t.Fatal(err)
	}
	var grant policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.identity"}, &grant); err != nil {
		t.Fatal(err)
	}
	if grant.Spec.Subject.Identity == nil || grant.Spec.Subject.Identity.Name != "test-worker" ||
		!slices.Equal(grant.Spec.PolicySets, []string{testExtraPolicy}) ||
		len(grant.OwnerReferences) != 1 || grant.OwnerReferences[0].UID != identity.UID {
		t.Errorf("the Identity grant is %+v owned by %v", grant.Spec, grant.OwnerReferences)
	}

	var binding policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.launcher"}, &binding); err != nil {
		t.Fatal(err)
	}
	if binding.Spec.Subject != (policyapi.Subject{Namespace: f.caller.Namespace, Name: f.caller.Name}) ||
		!slices.Equal(binding.Spec.PolicySets, []string{"test-worker.launcher"}) {
		t.Errorf("the launcher grant binds %+v", binding.Spec)
	}
	var set policyapi.PolicySet
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.launcher"}, &set); err != nil {
		t.Fatal(err)
	}
	for _, owned := range []client.Object{&binding, &set} {
		if owners := owned.GetOwnerReferences(); len(owners) != 1 || owners[0].UID != sandbox.UID {
			t.Errorf("%s is owned by %v, want the Sandbox alone", owned.GetName(), owners)
		}
	}

	var inbox, actions *policyapi.Rule
	for i, rule := range set.Spec.Rules {
		switch rule.Type {
		case inboxAccessRule:
			inbox = &set.Spec.Rules[i]
		case "action_auto_approve":
			actions = &set.Spec.Rules[i]
		}
	}
	if inbox == nil || !reflect.DeepEqual(inbox.Targets.Sandboxes, []policyapi.SandboxTarget{{Name: "test-worker", UID: sandbox.UID}}) {
		t.Errorf("the inbox rule is %+v, want send_message to this Sandbox alone", inbox)
	}
	if actions == nil || !reflect.DeepEqual(actions.Policy.Actions, map[string]policyapi.ActionNames{testGroup: {"create_session", "read_turns", "submit_input", "wait_for_turn"}}) {
		t.Fatalf("the Action rule is %+v, want the granted Actions of this group", actions)
	}
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(actions.Policy.Schema.Raw))
	if err != nil {
		t.Fatal(err)
	}
	rule, err := catalog.CompileSchema(document)
	if err != nil {
		t.Fatal(err)
	}
	for _, request := range []struct {
		sandbox contract.SandboxRef
		matches bool
	}{
		{contract.SandboxRef{Name: "test-worker", UID: sandbox.UID}, true},
		{contract.SandboxRef{Name: "test-worker", UID: "00000000-0000-0000-0000-000000000000"}, false},
		{contract.SandboxRef{Name: "test-other", UID: sandbox.UID}, false},
	} {
		arguments := map[string]any{"sandbox": map[string]any{"name": request.sandbox.Name, "uid": string(request.sandbox.UID)}, "text": "hi"}
		if matched := rule.Validate(arguments) == nil; matched != request.matches {
			t.Errorf("the rule matches %v: %t, want %t", request.sandbox, matched, request.matches)
		}
	}
}

func TestIdentityLauncherRetriesAndGrantsRemainBoundToItsIncarnation(t *testing.T) {
	f := newFixture(t)
	launcher := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Name: "test-launcher", Namespace: f.namespace},
		Spec:       policyapi.IdentitySpec{Description: "Test launcher."},
	}
	if err := kube.Create(t.Context(), launcher); err != nil {
		t.Fatal(err)
	}
	caller := store.IdentityCaller{Identity: store.Identity{Name: launcher.Name, UID: string(launcher.UID)}}
	launch := func(caller store.Caller) attempted {
		outcome, err := f.executor.attempt(t.Context(), store.ExecutionRequest{
			RequestID: uuid.New(), Action: store.ActionIdentity{Group: testGroup, Name: contract.ActionLaunchSandbox},
			Arguments: json.RawMessage(`{"name":"test-worker","template":"test-template","launcher_grants":["submit_input"]}`), Caller: caller,
		})
		return attempted{t, outcome, err}
	}
	first := resultOf[Launched](launch(caller))
	if again := resultOf[Launched](launch(caller)); again.Sandbox != first.Sandbox {
		t.Fatalf("retry launched %+v, want %+v", again.Sandbox, first.Sandbox)
	}
	var binding policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.launcher"}, &binding); err != nil {
		t.Fatal(err)
	}
	if binding.Spec.Subject.Identity == nil || binding.Spec.Subject.Identity.Name != launcher.Name || binding.Spec.Subject.Namespace != "" || binding.Spec.Subject.Name != "" {
		t.Fatalf("launcher grant subject = %+v", binding.Spec.Subject)
	}
	if len(binding.OwnerReferences) != 2 || binding.OwnerReferences[0].Kind != sandboxapi.SandboxKind || binding.OwnerReferences[0].UID != first.Sandbox.UID ||
		binding.OwnerReferences[1].Kind != "Identity" || binding.OwnerReferences[1].Name != launcher.Name || binding.OwnerReferences[1].UID != launcher.UID {
		t.Fatalf("launcher grant owners = %+v", binding.OwnerReferences)
	}
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker"}, &sandbox); err != nil {
		t.Fatal(err)
	}
	var intent struct {
		Caller  struct{ Namespace, Name string } `json:"caller"`
		Request struct {
			LaunchOrigin struct {
				CallerIdentity store.Identity `json:"caller_identity"`
			} `json:"launch_origin"`
		} `json:"request"`
	}
	if err := json.Unmarshal([]byte(sandbox.Annotations[sandboxapi.CreateIntentAnnotation]), &intent); err != nil {
		t.Fatal(err)
	}
	if intent.Caller.Namespace != f.namespace || intent.Caller.Name != "actions" || intent.Request.LaunchOrigin.CallerIdentity != caller.Identity {
		t.Fatalf("actor and effective launcher attribution = %+v", intent)
	}
	if err := kube.Delete(t.Context(), launcher); err != nil {
		t.Fatal(err)
	}
	replacement := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Name: launcher.Name, Namespace: f.namespace},
		Spec:       policyapi.IdentitySpec{Description: "Replacement test launcher."},
	}
	if err := kube.Create(t.Context(), replacement); err != nil {
		t.Fatal(err)
	}
	if kind := launch(caller).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ENVIRONMENT_REFUSED {
		t.Fatalf("stale Identity caller failure = %v", kind)
	}
	successor := store.IdentityCaller{Identity: store.Identity{Name: replacement.Name, UID: string(replacement.UID)}}
	if kind := launch(successor).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Fatalf("replacement Identity adopted old launch: %v", kind)
	}
	// No garbage collector runs in this fixture: the old grant must retain its original UID pin.
	var retained policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKeyFromObject(&binding), &retained); err != nil {
		t.Fatal(err)
	}
	if retained.OwnerReferences[1].UID != launcher.UID {
		t.Fatalf("replacement caller changed old grant: %+v", retained.OwnerReferences)
	}
}

func TestALaunchWithoutGrantsWritesNone(t *testing.T) {
	f := newFixture(t)
	if launched := f.launch(t, "test-worker"); launched.UID == "" {
		t.Fatal("launch answered no UID")
	}
	var sets policyapi.PolicySetList
	if err := kube.List(t.Context(), &sets, client.InNamespace(f.namespace)); err != nil {
		t.Fatal(err)
	}
	var bindings policyapi.PolicyBindingList
	if err := kube.List(t.Context(), &bindings, client.InNamespace(f.namespace)); err != nil {
		t.Fatal(err)
	}
	if len(sets.Items) != 2 || len(bindings.Items) != 0 {
		t.Errorf("after a launch without grants there are %d PolicySets and %d PolicyBindings, want the fixture's 2 sets and no bindings",
			len(sets.Items), len(bindings.Items))
	}
}

func TestRefusedChangedLaunchPreservesItsAdoptedIdentity(t *testing.T) {
	f := newFixture(t)
	launch := `{"name":"test-worker","template":"test-template","policy_sets":["test-extra-policy"]%s}`
	first := resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, fmt.Sprintf(launch, "")))
	key := client.ObjectKey{Namespace: f.namespace, Name: first.Sandbox.Name}
	var identity policyapi.Identity
	if err := kube.Get(t.Context(), key, &identity); err != nil {
		t.Fatal(err)
	}
	uid := identity.UID
	changed := fmt.Sprintf(launch, `,"bootstrap":"different launch"`)
	if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, changed).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Fatalf("changed launch failed %v, want sandbox_exists", kind)
	}
	if err := kube.Get(t.Context(), key, &identity); err != nil || identity.UID != uid {
		t.Fatalf("refused changed launch removed or replaced the adopted Identity: %v", err)
	}
	var binding policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.identity"}, &binding); err != nil {
		t.Fatal(err)
	}
	if len(binding.OwnerReferences) != 1 || binding.OwnerReferences[0].UID != uid {
		t.Fatalf("Identity binding owners = %v, want the original Identity", binding.OwnerReferences)
	}
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), key, &sandbox); err != nil || sandbox.UID != first.Sandbox.UID || sandbox.Spec.Identity == nil || sandbox.Spec.Identity.Name != identity.Name {
		t.Fatalf("refused changed launch altered the original Sandbox: %+v, %v", sandbox.Spec, err)
	}
}

func TestRefusedNewLaunchRollsBackItsNewIdentity(t *testing.T) {
	f := newFixture(t)
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "test-worker", Namespace: f.namespace},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template", Identity: &sandboxapi.IdentityRef{Name: "another-identity"}},
	}
	if err := kube.Create(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, `{"name":"test-worker","template":"test-template"}`).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Fatalf("new launch failed %v, want sandbox_exists", kind)
	}
	key := client.ObjectKeyFromObject(sandbox)
	if err := kube.Get(t.Context(), key, &policyapi.Identity{}); !apierrors.IsNotFound(err) {
		t.Fatalf("new Identity after refused launch: %v, want NotFound", err)
	}
	var preserved sandboxapi.Sandbox
	if err := kube.Get(t.Context(), key, &preserved); err != nil || preserved.UID != sandbox.UID {
		t.Fatalf("refused new launch altered the existing Sandbox: %v", err)
	}
}

type competingLaunchEnvironment struct {
	environmentpb.EnvironmentServiceClient
}

func (e competingLaunchEnvironment) CreateEnvironment(ctx context.Context, request *environmentpb.CreateEnvironmentRequest, options ...grpc.CallOption) (*identitypb.EnvironmentTarget, error) {
	competing := proto.Clone(request).(*environmentpb.CreateEnvironmentRequest)
	competing.Bootstrap = "competing launch"
	if _, err := e.EnvironmentServiceClient.CreateEnvironment(ctx, competing, options...); err != nil {
		return nil, err
	}
	return e.EnvironmentServiceClient.CreateEnvironment(ctx, request, options...)
}

func TestRefusedLaunchPreservesItsIdentityAdoptedByACompetingLaunch(t *testing.T) {
	f := newFixture(t)
	f.executor.environment = competingLaunchEnvironment{f.executor.environment}
	if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, `{"name":"test-worker","template":"test-template"}`).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Fatalf("losing launch failed %v, want sandbox_exists", kind)
	}
	key := client.ObjectKey{Namespace: f.namespace, Name: "test-worker"}
	var identity policyapi.Identity
	if err := kube.Get(t.Context(), key, &identity); err != nil {
		t.Fatalf("losing launch deleted the winning launch's Identity: %v", err)
	}
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), key, &sandbox); err != nil || sandbox.Spec.Identity == nil || sandbox.Spec.Identity.Name != identity.Name || sandbox.Spec.Bootstrap != "competing launch" {
		t.Fatalf("winning launch = %+v, %v", sandbox.Spec, err)
	}
}

// An attempt can end without its outcome, as when its lease is lost, and the caller then launches again: the same
// launch adopts the Sandbox and completes its grants, where no other caller or other launch can take it over.
func TestTheSameLaunchAdoptsItsSandboxAndAnotherIsRefused(t *testing.T) {
	f := newFixture(t)
	launch := `{"name": "test-worker", "template": "test-template", "policy_sets": [%s], "launcher_grants": ["send_message"]}`
	first := resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-extra-policy", "test-default-policy"`)))
	if err := kube.Delete(t.Context(), &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: "test-worker.launcher"}}); err != nil {
		t.Fatal(err)
	}
	again := resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-default-policy", "test-extra-policy"`)))
	if again.Sandbox != first.Sandbox {
		t.Errorf("the same launch answered %v, then %v", first.Sandbox, again.Sandbox)
	}
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.launcher"}, &policyapi.PolicyBinding{}); err != nil {
		t.Errorf("the same launch did not restore its launcher grant: %v", err)
	}
	other := store.ServiceAccount{Namespace: f.namespace, Name: "test-other-caller"}
	if kind := f.attempt(t, other, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-extra-policy"`)).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Errorf("another caller's launch of the name failed %v, want sandbox_exists", kind)
	}
	if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-default-policy"`)).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_EXISTS {
		t.Errorf("another launch of the name failed %v, want sandbox_exists", kind)
	}
}

func TestALaunchNamingAPolicySetItCannotBindIsRefused(t *testing.T) {
	f := newFixture(t)
	f.launch(t, "test-worker", contract.GrantSendMessage)
	for _, test := range []struct {
		policySet string
		kind      action_servicepb.ExecutionErrorKind
	}{
		{"test-absent-policy", action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_POLICY_SET},
		// Bound to the new Sandbox's account, it would give that account the first launcher's grant.
		{"test-worker.launcher", action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_LAUNCHER_POLICY_SET},
	} {
		arguments := fmt.Sprintf(`{"name": "test-second", "template": "test-template", "policy_sets": [%q]}`, test.policySet)
		if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, arguments).failure(); kind != test.kind {
			t.Errorf("a launch naming %s failed %v, want %v", test.policySet, kind, test.kind)
		}
	}
	err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-second"}, &sandboxapi.Sandbox{})
	if !apierrors.IsNotFound(err) {
		t.Errorf("reading the Sandbox of a refused launch: %v, want it absent", err)
	}
}

func TestSessionCommandsReachTheDispatcherForTheLaunchedIncarnationOnly(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	session, command := uuid.NewString(), uuid.NewString()
	sandbox := fmt.Sprintf(`{"name": %q, "uid": %q}`, launched.Name, launched.UID)
	opening := resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionCreateSession, fmt.Sprintf(`{
		"sandbox": %s, "session_id": %q, "setup_script": "true",
		"spec": {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/test/{session_id}", "instructions": "test"}
	}`, sandbox, session)))
	status, _ := opening["status"].(map[string]any)
	if opening["session_id"] != session || status["materialization"] != "SESSION_MATERIALIZATION_WAITING" {
		t.Errorf("create_session answered %v", opening)
	}
	dispatch := resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionSubmitInput,
		fmt.Sprintf(`{"sandbox": %s, "session_id": %q, "command_id": %q, "text": "hello"}`, sandbox, session, command)))
	if dispatch["command_id"] != command {
		t.Errorf("submit_input answered %v", dispatch)
	}
	want := []proto.Message{
		&dispatcherpb.CreateSessionRequest{
			SessionId: session, SetupScript: new("true"),
			Spec: &sessionpb.SessionSpec{
				Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: launched.Name, Uid: string(launched.UID)}, Harness: sessionpb.Harness_HARNESS_CODEX}}}, Model: "test-model", Cwd: "/test/" + session, Instructions: "test",
			},
		},
		&dispatcherpb.SubmitInputRequest{
			CommandId: command, SessionId: session, ExpectedSandboxUid: string(launched.UID), Text: "hello",
		},
	}
	if received := f.dispatcher.received(); !slices.EqualFunc(received, want, proto.Equal) {
		t.Errorf("the Dispatcher received %v, want %v", received, want)
	}

	replaced := fmt.Sprintf(`{"name": %q, "uid": "00000000-0000-0000-0000-000000000000"}`, launched.Name)
	absent := fmt.Sprintf(`{"name": "test-absent", "uid": %q}`, launched.UID)
	for target, kind := range map[string]action_servicepb.ExecutionErrorKind{replaced: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_REPLACED, absent: action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_FOUND} {
		arguments := fmt.Sprintf(`{"sandbox": %s, "session_id": %q, "command_id": %q, "turn_id": "test-turn"}`, target, session, command)
		if failed := f.attempt(t, f.caller, contract.ActionInterruptTurn, arguments).failure(); failed != kind {
			t.Errorf("interrupt_turn of %s failed %v, want %v", target, failed, kind)
		}
	}
	if received := f.dispatcher.received(); len(received) != len(want) {
		t.Errorf("the Dispatcher received %d requests for other incarnations", len(received)-len(want))
	}
}

func TestCentralSamplerHarnessReachesDispatcher(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	session := uuid.NewString()
	resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionCreateSession, fmt.Sprintf(`{
		"sandbox": {"name": %q, "uid": %q}, "session_id": %q,
		"spec": {"harness": "HARNESS_CENTRAL_SAMPLER", "model": "test-model", "cwd": "/test/{session_id}"}
	}`, launched.Name, launched.UID, session)))
	want := &dispatcherpb.CreateSessionRequest{
		SessionId: session,
		Spec: &sessionpb.SessionSpec{
			Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: launched.Name, Uid: string(launched.UID)}}}}, Model: "test-model", Cwd: "/test/" + session,
		},
	}
	if got := f.dispatcher.received(); len(got) != 1 || !proto.Equal(got[0], want) {
		t.Fatalf("dispatcher received %v, want %v", got, want)
	}
}

func TestADispatcherRefusalFailsAndAnyOtherErrorLeavesTheOutcomeUnknown(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	arguments := fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}, "session_id": %q, "command_id": %q, "text": "hello"}`,
		launched.Name, launched.UID, uuid.NewString(), uuid.NewString())
	f.dispatcher.refuse(status.Error(codes.AlreadyExists, "command_id was used for other work"))
	if kind := f.attempt(t, f.caller, contract.ActionSubmitInput, arguments).failure(); kind != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ID_REUSED {
		t.Errorf("a refused submit_input failed %q, want id_reused", kind)
	}
	f.dispatcher.refuse(status.Error(codes.Unavailable, "dispatcher storage unavailable"))
	if attempt := f.attempt(t, f.caller, contract.ActionSubmitInput, arguments); !errors.Is(attempt.err, worker.ErrOutcomeUnknown) {
		t.Errorf("an unavailable Dispatcher ended the attempt %s, %v; want its outcome unknown", attempt.outcome.State, attempt.err)
	}
}

func TestEnvironmentCreateUnavailableLeavesLaunchOutcomeUnknown(t *testing.T) {
	if err := launchError(status.Error(codes.Unavailable, "connection ended after create")); !errors.Is(err, worker.ErrOutcomeUnknown) {
		t.Fatalf("uncertain create = %v, want outcome unknown", err)
	}
}

// What list_launch_presets answers copies into launch_sandbox's and create_session's arguments as it is.
func TestThePresetsFillTheirActionsArguments(t *testing.T) {
	f := newFixture(t)
	listed := resultOf[PresetList](f.attempt(t, f.caller, contract.ActionListLaunchPresets, `{}`))
	preset, listedPreset := listed.Presets["test-preset"]
	if !listedPreset || len(listed.Presets) != 1 {
		t.Fatalf("list_launch_presets answered %v, want the test preset", listed)
	}
	launch, err := json.Marshal(preset.Launch)
	if err != nil {
		t.Fatal(err)
	}
	var arguments map[string]any
	if err := json.Unmarshal(launch, &arguments); err != nil {
		t.Fatal(err)
	}
	arguments["name"] = "test-worker"
	launchArguments, err := json.Marshal(arguments)
	if err != nil {
		t.Fatal(err)
	}
	launched := resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, string(launchArguments))).Sandbox
	session, err := json.Marshal(map[string]any{"sandbox": launched, "session_id": uuid.NewString(), "spec": preset.CreateSession.Spec})
	if err != nil {
		t.Fatal(err)
	}
	resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionCreateSession, string(session)))
}

func TestAnExecutorRefusesAPresetThatCannotFillItsActions(t *testing.T) {
	for name, presets := range map[string]string{
		"an unknown field": `{"p": {"title": "t", "launch": {"template": "t", "image": "i"},
			"create_session": {"spec": {"harness": "HARNESS_CODEX", "model": "m", "cwd": "/"}}}}`,
		"no harness": `{"p": {"title": "t", "launch": {"template": "t"}, "create_session": {"spec": {"model": "m", "cwd": "/"}}}}`,
		"no template": `{"p": {"title": "t", "launch": {},
			"create_session": {"spec": {"harness": "HARNESS_CLAUDE", "model": "m", "cwd": "/"}}}}`,
	} {
		t.Run(name, func(t *testing.T) {
			binding := catalog.OrchestrationBinding{Description: "d", Namespace: "test", Presets: json.RawMessage(presets)}
			if _, err := NewExecutor(testGroup, binding, kube, nil, nil, nil, 30*time.Second); err == nil {
				t.Error("NewExecutor() accepted the presets")
			}
		})
	}
}

// A launcher grant names an Action of this group by its name, and its rule matches the Action's `sandbox` argument.
func TestEveryGrantableActionIsOfferedAndTakesItsSandbox(t *testing.T) {
	f := newFixture(t)
	var launch struct {
		Properties struct {
			LauncherGrants struct {
				Items struct {
					Enum []string `json:"enum"`
				} `json:"items"`
			} `json:"launcher_grants"`
		} `json:"properties"`
	}
	offered := f.executor.Actions()
	if err := json.Unmarshal(offered[contract.ActionLaunchSandbox].InputSchema, &launch); err != nil {
		t.Fatal(err)
	}
	grantable := slices.DeleteFunc(launch.Properties.LauncherGrants.Items.Enum, func(grant string) bool { return grant == contract.GrantSendMessage })
	wantActions := f.executor.inputs.SandboxActions()
	if !slices.Equal(slices.Sorted(slices.Values(grantable)), slices.Sorted(slices.Values(wantActions))) {
		t.Errorf("launcher_grants offers %q, want this group's Actions on one Sandbox, %q", grantable, wantActions)
	}
	for _, action := range wantActions {
		var schema struct {
			Required []string `json:"required"`
		}
		if err := json.Unmarshal(offered[action].InputSchema, &schema); err != nil {
			t.Fatal(err)
		}
		if !slices.Contains(schema.Required, "sandbox") {
			t.Errorf("%s does not require the sandbox a launcher grant matches", action)
		}
	}
}
