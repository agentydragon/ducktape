package orchestrationv1

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"github.com/santhosh-tekuri/jsonschema/v6"
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
		"launcher_grants": ["submit_input", "send_message", "open_session", "read_turns", "wait_for_turn"]
	}`))
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker"}, &sandbox); err != nil {
		t.Fatal(err)
	}
	if launched.Sandbox != (contract.SandboxRef{Name: "test-worker", UID: sandbox.UID}) {
		t.Errorf("launch answered %v, want the Sandbox %s", launched.Sandbox, sandbox.UID)
	}
	if want := []string{"open_session", "read_turns", "send_message", "submit_input", "wait_for_turn"}; !slices.Equal(launched.LauncherGrants, want) {
		t.Errorf("launch granted %q, want %q", launched.LauncherGrants, want)
	}
	spec := sandbox.Spec
	if spec.Template != "test-template" || !spec.Running || !spec.WakeOnSend || spec.Bootstrap != "true" ||
		spec.IdleSuspendAfter == nil || spec.IdleSuspendAfter.Duration != 10*time.Minute ||
		!slices.Equal(spec.PolicySets, []string{testDefaultPolicy, testExtraPolicy}) || !slices.Equal(spec.InboxTags, []string{"test-tag"}) {
		t.Errorf("the Sandbox's spec is %+v", spec)
	}

	var binding policyapi.PolicyBinding
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: "test-worker.launcher"}, &binding); err != nil {
		t.Fatal(err)
	}
	if binding.Spec.Subject != (policyapi.ServiceAccountRef{Namespace: f.caller.Namespace, Name: f.caller.Name}) ||
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
	if actions == nil || !reflect.DeepEqual(actions.Policy.Actions, map[string]policyapi.ActionNames{testGroup: {"open_session", "read_turns", "submit_input", "wait_for_turn"}}) {
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
		t.Errorf("after a launch without grants there are %d PolicySets and %d PolicyBindings, want the fixture's 2 and none",
			len(sets.Items), len(bindings.Items))
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
	if kind := f.attempt(t, other, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-extra-policy"`)).failure(); kind != "sandbox_exists" {
		t.Errorf("another caller's launch of the name failed %q, want sandbox_exists", kind)
	}
	if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, fmt.Sprintf(launch, `"test-default-policy"`)).failure(); kind != "sandbox_exists" {
		t.Errorf("another launch of the name failed %q, want sandbox_exists", kind)
	}
}

func TestALaunchNamingAPolicySetItCannotBindIsRefused(t *testing.T) {
	f := newFixture(t)
	f.launch(t, "test-worker", contract.GrantSendMessage)
	for _, test := range []struct{ policySet, kind string }{
		{"test-absent-policy", "unknown_policy_set"},
		// Bound to the new Sandbox's account, it would give that account the first launcher's grant.
		{"test-worker.launcher", "launcher_policy_set"},
	} {
		arguments := fmt.Sprintf(`{"name": "test-second", "template": "test-template", "policy_sets": [%q]}`, test.policySet)
		if kind := f.attempt(t, f.caller, contract.ActionLaunchSandbox, arguments).failure(); kind != test.kind {
			t.Errorf("a launch naming %s failed %q, want %s", test.policySet, kind, test.kind)
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
	opening := resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionOpenSession, fmt.Sprintf(`{
		"sandbox": %s, "session_id": %q, "setup_script": "true",
		"spec": {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/test/{session_id}", "instructions": "test"}
	}`, sandbox, session)))
	if opening["session_id"] != session || opening["state"] != "DELIVERY_STATE_QUEUED_IN_DISPATCHER" {
		t.Errorf("open_session answered %v", opening)
	}
	dispatch := resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionSubmitInput,
		fmt.Sprintf(`{"sandbox": %s, "session_id": %q, "command_id": %q, "text": "hello"}`, sandbox, session, command)))
	if dispatch["command_id"] != command {
		t.Errorf("submit_input answered %v", dispatch)
	}
	want := []proto.Message{
		&dispatcherpb.OpenSessionRequest{
			SessionId: session, Sandbox: launched.Name, SetupScript: new("true"),
			Spec: &runnerpb.SessionSpec{
				Harness: runnerpb.Harness_HARNESS_CODEX, Model: "test-model", Cwd: "/test/" + session, Instructions: "test",
			},
		},
		&dispatcherpb.SubmitInputRequest{
			CommandId: command, SessionId: session, Sandbox: launched.Name, SandboxUid: string(launched.UID), Text: "hello",
		},
	}
	if received := f.dispatcher.received(); !slices.EqualFunc(received, want, proto.Equal) {
		t.Errorf("the Dispatcher received %v, want %v", received, want)
	}

	replaced := fmt.Sprintf(`{"name": %q, "uid": "00000000-0000-0000-0000-000000000000"}`, launched.Name)
	absent := fmt.Sprintf(`{"name": "test-absent", "uid": %q}`, launched.UID)
	for target, kind := range map[string]string{replaced: "sandbox_replaced", absent: "sandbox_not_found"} {
		arguments := fmt.Sprintf(`{"sandbox": %s, "session_id": %q, "command_id": %q, "turn_id": "test-turn"}`, target, session, command)
		if failed := f.attempt(t, f.caller, contract.ActionInterruptTurn, arguments).failure(); failed != kind {
			t.Errorf("interrupt_turn of %s failed %q, want %s", target, failed, kind)
		}
	}
	if received := f.dispatcher.received(); len(received) != len(want) {
		t.Errorf("the Dispatcher received %d requests for other incarnations", len(received)-len(want))
	}
}

func TestADispatcherRefusalFailsAndAnyOtherErrorLeavesTheOutcomeUnknown(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	arguments := fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}, "session_id": %q, "command_id": %q, "text": "hello"}`,
		launched.Name, launched.UID, uuid.NewString(), uuid.NewString())
	f.dispatcher.refuse(status.Error(codes.AlreadyExists, "command_id was used for other work"))
	if kind := f.attempt(t, f.caller, contract.ActionSubmitInput, arguments).failure(); kind != "id_reused" {
		t.Errorf("a refused submit_input failed %q, want id_reused", kind)
	}
	f.dispatcher.refuse(status.Error(codes.Unavailable, "dispatcher storage unavailable"))
	if attempt := f.attempt(t, f.caller, contract.ActionSubmitInput, arguments); !errors.Is(attempt.err, worker.ErrOutcomeUnknown) {
		t.Errorf("an unavailable Dispatcher ended the attempt %s, %v; want its outcome unknown", attempt.outcome.State, attempt.err)
	}
}

// What list_launch_presets answers copies into launch_sandbox's and open_session's arguments as it is.
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
	session, err := json.Marshal(map[string]any{"sandbox": launched, "session_id": uuid.NewString(), "spec": preset.OpenSession.Spec})
	if err != nil {
		t.Fatal(err)
	}
	resultOf[map[string]any](f.attempt(t, f.caller, contract.ActionOpenSession, string(session)))
}

func TestAnExecutorRefusesAPresetThatCannotFillItsActions(t *testing.T) {
	for name, presets := range map[string]string{
		"an unknown field": `{"p": {"title": "t", "launch": {"template": "t", "image": "i"},
			"open_session": {"spec": {"harness": "HARNESS_CODEX", "model": "m", "cwd": "/"}}}}`,
		"no harness": `{"p": {"title": "t", "launch": {"template": "t"}, "open_session": {"spec": {"model": "m", "cwd": "/"}}}}`,
		"no template": `{"p": {"title": "t", "launch": {},
			"open_session": {"spec": {"harness": "HARNESS_CLAUDE", "model": "m", "cwd": "/"}}}}`,
	} {
		t.Run(name, func(t *testing.T) {
			binding := catalog.OrchestrationBinding{Description: "d", Namespace: "test", Presets: json.RawMessage(presets)}
			if _, err := NewExecutor(testGroup, binding, kube, nil, nil, 30*time.Second); err == nil {
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
