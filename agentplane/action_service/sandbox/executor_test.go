package sandbox

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// attempted is how one attempt ended.
type attempted struct {
	t       *testing.T
	outcome store.ExecutionOutcome
	err     error
}

// attempt runs one attempt at action as caller. Execute is attempt under worker.Hold, whose lease only the worker
// makes, once the caller is one a box can be given.
func (f fixture) attempt(t *testing.T, caller action.ServiceAccount, action, arguments string) attempted {
	t.Helper()
	executor, err := NewExecutor(f.binding, f.inventory())
	if err != nil {
		t.Fatal(err)
	}
	outcome, err := executor.attempt(t.Context(), store.ExecutionRequest{
		RequestID: uuid.New(), Action: store.ActionIdentity{Group: "sandbox", Name: action},
		Arguments: json.RawMessage(arguments), Caller: caller,
	})
	return attempted{t, outcome, err}
}

// resultOf is the result of an attempt that succeeded.
func resultOf[T any](a attempted) T {
	a.t.Helper()
	if a.err != nil || a.outcome.State != store.ExecutionSucceeded {
		a.t.Fatalf("attempt = %s %s, %v; want it to succeed", a.outcome.State, a.outcome.Error, a.err)
	}
	var result T
	if err := json.Unmarshal(a.outcome.Result, &result); err != nil {
		a.t.Fatal(err)
	}
	return result
}

// failure is the kind of an attempt that failed with a reason the caller can act on.
func (a attempted) failure() string {
	a.t.Helper()
	if a.err != nil || a.outcome.State != store.ExecutionFailed {
		a.t.Fatalf("attempt = %s %s, %v; want it to fail", a.outcome.State, a.outcome.Result, a.err)
	}
	var failure struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(a.outcome.Error, &failure); err != nil {
		a.t.Fatal(err)
	}
	return failure.Kind
}

// The identity comes from the authenticated request and never from an argument, so a caller cannot reach another
// account's boxes by asking for them.
func TestEveryActionActsAsTheRequestCaller(t *testing.T) {
	f := newFixture(t, nil)
	other := account(t, f.binding.Namespace, "test-other")
	for _, caller := range []action.ServiceAccount{f.caller, other} {
		resultOf[Info](f.attempt(t, caller, actionCreate, `{"name": "box", "template": "test-template"}`))
		if pod := f.pod(t, caller, "box"); pod.Spec.ServiceAccountName != caller.Name {
			t.Errorf("%s's box runs as %q", caller.Name, pod.Spec.ServiceAccountName)
		}
	}
	if listed := resultOf[SandboxList](f.attempt(t, f.caller, actionList, `{}`)); len(listed.Sandboxes) != 1 {
		t.Errorf("list = %v, want the caller's one box", listed)
	}
	if disposed := resultOf[DisposeResult](f.attempt(t, f.caller, actionDispose, `{"name": "box"}`)); !disposed.Existed {
		t.Errorf("dispose = %v, want the caller's box disposed of", disposed)
	}
	resultOf[Info](f.attempt(t, other, actionGet, `{"name": "box"}`))
}

// No argument names an account: one that tries is refused rather than quietly ignored.
func TestAnAccountNamedInArgumentsIsNotRead(t *testing.T) {
	f := newFixture(t, nil)
	other := account(t, f.binding.Namespace, "test-other")
	arguments := `{"name": "box", "template": "test-template", "caller": "test-other", "namespace": "` + f.binding.Namespace + `"}`
	if kind := f.attempt(t, f.caller, actionCreate, arguments).failure(); kind != "invalid_arguments" {
		t.Errorf("kind = %q, want invalid_arguments", kind)
	}
	for _, caller := range []action.ServiceAccount{f.caller, other} {
		if boxes, err := f.inventory().List(t.Context(), caller); err != nil || len(boxes) != 0 {
			t.Errorf("%s has boxes %v, %v", caller.Name, boxes, err)
		}
	}
}

// A Pod runs as an account of its own namespace or not at all, so a caller from elsewhere cannot be given a box that
// is it, and must not be given one that is somebody else.
func TestACallerFromAnotherNamespaceIsRefused(t *testing.T) {
	f := newFixture(t, nil)
	elsewhere := account(t, f.binding.Namespace+"-elsewhere", f.caller.Name)
	executor, err := NewExecutor(f.binding, f.inventory())
	if err != nil {
		t.Fatal(err)
	}
	// The refusal comes before any lease is held, so it needs none.
	outcome, err := executor.Execute(t.Context(), store.ExecutionRequest{
		RequestID: uuid.New(), Action: store.ActionIdentity{Group: "sandbox", Name: actionCreate},
		Arguments: json.RawMessage(`{"name": "box", "template": "test-template"}`), Caller: elsewhere,
	}, nil)
	if kind := (attempted{t, outcome, err}).failure(); kind != "caller_not_local" {
		t.Errorf("kind = %q, want caller_not_local", kind)
	}
	pods, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).List(t.Context(), metav1.ListOptions{})
	if err != nil || len(pods.Items) != 0 {
		t.Errorf("the namespace holds Pods %v, %v", pods, err)
	}
}

// A command that ran and failed is an answer: the Action did what it was asked.
func TestANonzeroExitIsAResult(t *testing.T) {
	f := newFixture(t, nil)
	resultOf[Info](f.attempt(t, f.caller, actionCreate, `{"name": "box", "template": "test-template"}`))
	f.report(t, f.caller, "box", ready)
	result := resultOf[map[string]any](f.attempt(t, f.caller, actionExec,
		`{"name": "box", "script": "echo out; echo err >&2; exit 3", "timeout_seconds": 60, "max_output_bytes": 1000}`))
	if _, timed := result["duration_seconds"].(float64); !timed {
		t.Errorf("result %v has no duration", result)
	}
	delete(result, "duration_seconds")
	want := map[string]any{"exit": map[string]any{"kind": "exited", "exit_code": 3.0}, "stdout": "out\n", "stderr": "err\n"}
	if !reflect.DeepEqual(result, want) {
		t.Errorf("result = %v, want %v", result, want)
	}
}

// A request admitted under other caps, across a settings roll, is cut to this group's rather than refused.
func TestAnExecIsCutToThisGroupsCaps(t *testing.T) {
	f := newFixture(t, nil)
	f.binding.MaxTimeoutSeconds, f.binding.MaxOutputBytes = 1, 4
	resultOf[Info](f.attempt(t, f.caller, actionCreate, `{"name": "box", "template": "test-template"}`))
	f.report(t, f.caller, "box", ready)
	for _, test := range []struct {
		script string
		field  string
		want   map[string]any
	}{
		{"sleep 60", "exit", map[string]any{"kind": "timed_out"}},
		{"printf 0123456789", "stdout", map[string]any{"truncated_text": "0123", "total_bytes": 10.0}},
	} {
		result := resultOf[map[string]any](f.attempt(t, f.caller, actionExec,
			`{"name": "box", "script": "`+test.script+`", "timeout_seconds": 3600, "max_output_bytes": 1000000}`))
		if !reflect.DeepEqual(result[test.field], test.want) {
			t.Errorf("%q ran to %v, want %s %v", test.script, result, test.field, test.want)
		}
	}
}

func TestARefusalBecomesAReasonTheCallerCanActOn(t *testing.T) {
	f := newFixture(t, nil)
	// No node runs a box here, so the API server has no kubelet to hand an exec to.
	f.exec.run = KubernetesExec(server.Config, server.Clientset)
	for _, name := range []string{"box", "unready"} {
		resultOf[Info](f.attempt(t, f.caller, actionCreate, `{"name": "`+name+`", "template": "test-template"}`))
	}
	f.report(t, f.caller, "box", ready)
	other := account(t, f.binding.Namespace, "test-other")
	if _, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).Create(t.Context(), &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: objectName(other, "taken"), Labels: map[string]string{nameLabel: "taken"}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "workspace", Image: "registry.test/unused"}}},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	run := func(name string) string {
		return `{"name": "` + name + `", "script": "true", "timeout_seconds": 5, "max_output_bytes": 0}`
	}
	for _, test := range []struct {
		name      string
		caller    action.ServiceAccount
		action    string
		arguments string
		kind      string
	}{
		{"an absent box", f.caller, actionGet, `{"name": "absent"}`, "sandbox_unavailable"},
		{"a template not offered", f.caller, actionCreate, `{"name": "box", "template": "unoffered"}`, "sandbox_unavailable"},
		{"a box that is not ready", f.caller, actionExec, run("unready"), "sandbox_unavailable"},
		{"a Pod that is not the caller's", other, actionGet, `{"name": "taken"}`, "sandbox_not_yours"},
		{"an exec Kubernetes cannot run", f.caller, actionExec, run("box"), "exec_failed"},
		{"an Action this executor lacks", f.caller, "unoffered", `{}`, "unknown_action"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if kind := f.attempt(t, test.caller, test.action, test.arguments).failure(); kind != test.kind {
				t.Errorf("kind = %q, want %q", kind, test.kind)
			}
		})
	}
}

// Only an outcome this executor can characterise becomes a failed Execution; anything else is the worker's to fail
// by its type.
func TestAnUnexpectedFailureIsNotSwallowed(t *testing.T) {
	f := newFixture(t, nil)
	f.binding.Templates["missing-template"] = "a box with no template"
	if attempt := f.attempt(t, f.caller, actionGetTemplate, `{"template": "missing-template"}`); !apierrors.IsNotFound(attempt.err) {
		t.Errorf("attempt = %v, %v; want the API server's NotFound", attempt.outcome, attempt.err)
	}
}

// Not a summary wrapped around it: the agent reads the Kubernetes object it already knows.
func TestATemplateComesBackAsThePodItself(t *testing.T) {
	f := newFixture(t, nil)
	stored, err := server.Clientset.CoreV1().PodTemplates(f.binding.Namespace).Get(t.Context(), testTemplate, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	shown := resultOf[corev1.PodTemplateSpec](f.attempt(t, f.caller, actionGetTemplate, `{"template": "test-template"}`))
	if !reflect.DeepEqual(shown, stored.Template) {
		t.Errorf("get_template = %#v, want the stored template %#v", shown, stored.Template)
	}
}
