package management

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/policy"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/internal/view"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/protobuf/types/known/durationpb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const namespace = "test-sandboxes"

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	return scheme
}

type assigningClient struct {
	client.Client
	created int
}

func (c *assigningClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if sandbox, ok := object.(*sandboxapi.Sandbox); ok {
		c.created++
		sandbox.UID = types.UID(fmt.Sprintf("created-%d", c.created))
		sandbox.CreationTimestamp = metav1.NewTime(time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC))
	}
	return c.Client.Create(ctx, object, options...)
}

func newAccess(t *testing.T, objects ...client.Object) (*Access, client.Client) {
	t.Helper()
	underlying := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(objects...).Build()
	objectsClient := &assigningClient{Client: underlying}
	access, err := New(namespace, objectsClient, []string{"default-policy"}, &identitypb.ServiceAccount{
		Namespace: namespace, Name: "app",
	})
	if err != nil {
		t.Fatal(err)
	}
	return access, objectsClient
}

func testSandbox(name, uid string, mode sandboxapi.OperatingMode) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: name, UID: types.UID(uid),
			CreationTimestamp: metav1.NewTime(time.Date(2026, 10, 20, 1, 2, 3, 0, time.UTC)),
		},
		Spec:   sandboxapi.SandboxSpec{Template: "pod", Running: mode == sandboxapi.OperatingModeRunning},
		Status: sandboxapi.SandboxStatus{OperatingMode: mode},
	}
}

func TestCreateAppliesDefaultsAndCallerIntentAndIsIdempotent(t *testing.T) {
	base := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(
		&policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "default-policy", Namespace: namespace}},
		&policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "extra-policy", Namespace: namespace}},
	).Build()
	objects := &assigningClient{Client: base}
	caller := &identitypb.ServiceAccount{Namespace: namespace, Name: "app"}
	access, err := New(namespace, objects, []string{"default-policy", "default-policy"}, caller)
	if err != nil {
		t.Fatal(err)
	}
	preset := "selected-preset"
	request := &environmentpb.CreateEnvironmentRequest{
		Name: "created-sandbox", Template: "pod-template", Bootstrap: "echo < start",
		PolicySets: []string{"extra-policy"}, Preset: &preset, WakeOnSend: true,
		IdleSuspendAfter: durationpb.New(time.Minute), InboxTags: []string{"team-b", "team-a", "team-b"},
	}
	created, err := access.Create(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if created.Name != request.Name || created.Uid != "created-1" || created.Namespace != namespace {
		t.Fatalf("created target = %#v", created)
	}
	stored := &sandboxapi.Sandbox{}
	if err := objects.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: request.Name}, stored); err != nil {
		t.Fatal(err)
	}
	if stored.Spec.Template != request.Template || !stored.Spec.Running || !stored.Spec.WakeOnSend ||
		fmt.Sprint(stored.Spec.PolicySets) != "[default-policy extra-policy]" ||
		fmt.Sprint(stored.Spec.InboxTags) != "[team-a team-b]" || stored.Spec.IdleSuspendAfter.Duration != time.Minute {
		t.Fatalf("stored create spec = %#v", stored.Spec)
	}
	if stored.Labels[view.PresetLabel] != preset || stored.Annotations[sandboxapi.CreateIntentAnnotation] == "" {
		t.Fatalf("stored metadata = %#v", stored.ObjectMeta)
	}
	wantIntent := `{"caller":{"name":"app","namespace":"test-sandboxes"},"request":{"bootstrap":"echo < start","idle_suspend_after":"60s","inbox_tags":["team-b","team-a","team-b"],"name":"created-sandbox","policy_sets":["extra-policy"],"preset":"selected-preset","template":"pod-template","wake_on_send":true}}`
	if got := stored.Annotations[sandboxapi.CreateIntentAnnotation]; got != wantIntent {
		t.Fatalf("create intent = %s, want %s", got, wantIntent)
	}
	retry, err := access.Create(context.Background(), request)
	if err != nil || retry.GetUid() != created.GetUid() {
		t.Fatalf("same Create retry = %#v, %v", retry, err)
	}
	otherCaller, err := New(namespace, objects, nil, &identitypb.ServiceAccount{Namespace: namespace, Name: "other"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := otherCaller.Create(context.Background(), request); !isConflict(err) {
		t.Fatalf("different caller adopted name: %v", err)
	}
}

func TestCreateRejectsUnknownPolicySetsAndInvalidRequests(t *testing.T) {
	access, _ := newAccess(t)
	_, err := access.Create(context.Background(), &environmentpb.CreateEnvironmentRequest{Name: "unknown-policy", Template: "pod"})
	var unknown *policy.UnknownPolicySetError
	if !errors.As(err, &unknown) || fmt.Sprint(unknown.Names) != "[default-policy]" {
		t.Fatalf("Create with unknown default = %v", err)
	}
	emptyPreset := ""
	nonASCIIPreset := "ašb"
	for _, request := range []*environmentpb.CreateEnvironmentRequest{
		{Name: "Upper", Template: "pod"},
		{Name: "ašb", Template: "pod"},
		{Name: "valid-name", Template: ""},
		{Name: "empty-preset", Template: "pod", Preset: &emptyPreset},
		{Name: "unicode-preset", Template: "pod", Preset: &nonASCIIPreset},
		{Name: "valid-name", Template: "pod", Bootstrap: string(make([]byte, maxBootstrapCharacters+1))},
	} {
		if _, err := access.Create(context.Background(), request); !isInvalid(err) {
			t.Errorf("Create(%q) = %v, want InvalidSandboxError", request.GetName(), err)
		}
	}
}

func TestSuspendAndPolicyUpdatesKeepUIDFenceAndWakeGeneration(t *testing.T) {
	resource := testSandbox("paused", "sandbox-uid", sandboxapi.OperatingModeSuspended)
	resource.Spec.Running = false
	resource.Spec.WakeGeneration = 4
	access, objects := newAccess(t, resource)

	if err := access.Suspend(context.Background(), "paused", "old-uid"); !isNotFound(err) {
		t.Fatalf("stale suspend = %v, want NotFound", err)
	}
	if err := access.Suspend(context.Background(), "paused", "sandbox-uid"); err != nil {
		t.Fatal(err)
	}
	stored := readSandbox(t, objects, "paused")
	if stored.Spec.Running || stored.Spec.WakeGeneration != 4 {
		t.Fatalf("suspend state = running %v, generation %d", stored.Spec.Running, stored.Spec.WakeGeneration)
	}
	if err := access.SetWakeOnSend(context.Background(), "paused", "old-uid", false); !isNotFound(err) {
		t.Fatalf("stale wake policy update = %v", err)
	}
	if err := access.SetWakeOnSend(context.Background(), "paused", "sandbox-uid", true); err != nil {
		t.Fatal(err)
	}
	if !readSandbox(t, objects, "paused").Spec.WakeOnSend {
		t.Fatal("wake policy did not update")
	}
	if err := access.SetIdleSuspendAfter(context.Background(), "paused", "sandbox-uid", durationpb.New(2*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if got := readSandbox(t, objects, "paused").Spec.IdleSuspendAfter.Duration; got != 2*time.Minute {
		t.Fatalf("idle suspend duration = %v", got)
	}
	if err := access.SetIdleSuspendAfter(context.Background(), "paused", "sandbox-uid", nil); err != nil {
		t.Fatal(err)
	}
	if readSandbox(t, objects, "paused").Spec.IdleSuspendAfter != nil {
		t.Fatal("nil idle suspend policy did not clear field")
	}
}

func TestMutationsRefuseTeardown(t *testing.T) {
	deleting := metav1.NewTime(time.Now())
	resource := testSandbox("deleting", "uid-delete", sandboxapi.OperatingModeSuspended)
	resource.DeletionTimestamp = &deleting
	resource.Finalizers = []string{"test-finalizer"}
	access, _ := newAccess(t, resource)
	var tearingDown *SandboxTearingDownError
	if err := access.SetWakeOnSend(context.Background(), "deleting", "uid-delete", true); !errors.As(err, &tearingDown) {
		t.Fatalf("wake policy during deletion = %v", err)
	}
}

func TestDeleteRequiresReportedSuspensionAndUsesUIDFence(t *testing.T) {
	running := testSandbox("running", "uid-running", sandboxapi.OperatingModeRunning)
	unknown := testSandbox("unknown", "uid-unknown", "")
	paused := testSandbox("paused", "uid-paused", sandboxapi.OperatingModeSuspended)
	access, objects := newAccess(t, running, unknown, paused)
	var runningError *SandboxRunningError
	if err := access.Delete(context.Background(), "running", "uid-running"); !errors.As(err, &runningError) {
		t.Fatalf("delete running Sandbox = %v", err)
	}
	var modeError *SandboxModeUnknownError
	if err := access.Delete(context.Background(), "unknown", "uid-unknown"); !errors.As(err, &modeError) {
		t.Fatalf("delete unknown-mode Sandbox = %v", err)
	}
	if err := access.Delete(context.Background(), "paused", "old-uid"); !isNotFound(err) {
		t.Fatalf("stale delete = %v, want NotFound", err)
	}
	if err := access.Delete(context.Background(), "paused", "uid-paused"); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: "paused"}, &sandboxapi.Sandbox{}); !apierrors.IsNotFound(err) {
		t.Fatalf("deleted Sandbox still exists: %v", err)
	}
}

func TestPatchConflictRetriesAndRechecksUID(t *testing.T) {
	resource := testSandbox("replaceable", "old-uid", sandboxapi.OperatingModeRunning)
	underlying := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(resource).Build()
	conflicts := &conflictOnceClient{Client: underlying}
	access, err := New(namespace, conflicts, nil, &identitypb.ServiceAccount{Namespace: namespace, Name: "app"})
	if err != nil {
		t.Fatal(err)
	}
	if err := access.SetWakeOnSend(context.Background(), "replaceable", "old-uid", true); err != nil {
		t.Fatal(err)
	}
	if conflicts.patches != 2 || !readSandbox(t, underlying, "replaceable").Spec.WakeOnSend {
		t.Fatalf("conflict retry patch count=%d stored=%#v", conflicts.patches, readSandbox(t, underlying, "replaceable").Spec)
	}

	underlying = clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(resource.DeepCopy()).Build()
	replacing := &replaceBeforeRetryClient{Client: underlying}
	access, err = New(namespace, replacing, nil, &identitypb.ServiceAccount{Namespace: namespace, Name: "app"})
	if err != nil {
		t.Fatal(err)
	}
	if err := access.SetWakeOnSend(context.Background(), "replaceable", "old-uid", true); !isNotFound(err) {
		t.Fatalf("mutation crossed a same-name UID replacement: %v", err)
	}
	if got := readSandbox(t, underlying, "replaceable"); string(got.UID) != "new-uid" || got.Spec.WakeOnSend {
		t.Fatalf("replacement changed: uid=%s spec=%#v", got.UID, got.Spec)
	}
}

type conflictOnceClient struct {
	client.Client
	patches int
}

func (c *conflictOnceClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	c.patches++
	if c.patches == 1 {
		return apierrors.NewConflict(sandboxapi.GroupVersion.WithResource("sandboxes").GroupResource(), object.GetName(), errors.New("test conflict"))
	}
	return c.Client.Patch(ctx, object, patch, options...)
}

type replaceBeforeRetryClient struct {
	client.Client
	replaced bool
}

func (c *replaceBeforeRetryClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	if !c.replaced {
		c.replaced = true
		current := &sandboxapi.Sandbox{}
		key := client.ObjectKeyFromObject(object)
		if err := c.Client.Get(ctx, key, current); err != nil {
			return err
		}
		current.UID = "new-uid"
		if err := c.Client.Update(ctx, current); err != nil {
			return err
		}
		return apierrors.NewConflict(sandboxapi.GroupVersion.WithResource("sandboxes").GroupResource(), object.GetName(), errors.New("replacement race"))
	}
	return c.Client.Patch(ctx, object, patch, options...)
}

func readSandbox(t *testing.T, objects client.Client, name string) *sandboxapi.Sandbox {
	t.Helper()
	resource := &sandboxapi.Sandbox{}
	if err := objects.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: name}, resource); err != nil {
		t.Fatal(err)
	}
	return resource
}

func isNotFound(err error) bool {
	var target *SandboxNotFoundError
	return errors.As(err, &target)
}

func isConflict(err error) bool {
	var target *SandboxConflictError
	return errors.As(err, &target)
}

func isInvalid(err error) bool {
	var target *InvalidSandboxError
	return errors.As(err, &target)
}

func TestPythonJSONEscapingMatchesCreateIntentConvention(t *testing.T) {
	value := map[string]any{"z": "snowman ☃", "a": "<tag>"}
	got, err := pythonJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"a":"<tag>","z":"snowman \u2603"}`
	if got != want {
		t.Fatalf("pythonJSON = %s, want %s", got, want)
	}
	var decoded map[string]any
	if err := json.Unmarshal([]byte(got), &decoded); err != nil || decoded["a"] != "<tag>" {
		t.Fatalf("canonical JSON did not round trip: %v %v", decoded, err)
	}
}

func TestListTemplates(t *testing.T) {
	access, _ := newAccess(t,
		&sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "z-template", Namespace: namespace}},
		&sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "a-template", Namespace: namespace}})
	names, err := access.ListTemplates(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(names) != "[a-template z-template]" {
		t.Fatalf("templates = %v", names)
	}
}

type createOutcomeClient struct {
	client.Client
	outcome func(context.Context, client.Object) error
}

func (c *createOutcomeClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if err := c.Client.Create(ctx, object, options...); err != nil {
		return err
	}
	return c.outcome(ctx, object)
}

func TestCreateReturnsPersistedIdentityAcrossUncertainWritesAndReplacement(t *testing.T) {
	for _, test := range []struct {
		name      string
		uncertain bool
		replace   bool
	}{
		{name: "acknowledged write"},
		{name: "persisted write with lost acknowledgement", uncertain: true},
		{name: "same-name replacement after acknowledged write", replace: true},
		{name: "same-name replacement after uncertain write", uncertain: true, replace: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			underlying := clientfake.NewClientBuilder().WithScheme(testScheme(t)).Build()
			assigning := &assigningClient{Client: underlying}
			objects := &createOutcomeClient{Client: assigning}
			objects.outcome = func(ctx context.Context, object client.Object) error {
				if test.replace {
					if err := underlying.Delete(ctx, object); err != nil {
						return err
					}
					replacement := object.DeepCopyObject().(*sandboxapi.Sandbox)
					replacement.ResourceVersion = ""
					replacement.UID = "replacement-uid"
					replacement.Annotations = nil
					if err := underlying.Create(ctx, replacement); err != nil {
						return err
					}
				}
				if test.uncertain {
					return context.DeadlineExceeded
				}
				return nil
			}
			access, err := New(namespace, objects, nil, &identitypb.ServiceAccount{Namespace: namespace, Name: "app"})
			if err != nil {
				t.Fatal(err)
			}
			target, err := access.Create(t.Context(), &environmentpb.CreateEnvironmentRequest{Name: "created", Template: "pod"})
			if test.uncertain && test.replace {
				if target != nil || !isConflict(err) {
					t.Fatalf("uncertain write with foreign replacement = %v, %v", target, err)
				}
				return
			}
			if err != nil || target.GetUid() != "created-1" || target.GetNamespace() != namespace || target.GetName() != "created" {
				t.Fatalf("created target = %v, %v", target, err)
			}
			if test.replace {
				stored := &sandboxapi.Sandbox{}
				if err := underlying.Get(t.Context(), client.ObjectKey{Namespace: namespace, Name: "created"}, stored); err != nil {
					t.Fatal(err)
				}
				if string(stored.UID) == target.GetUid() {
					t.Fatal("same-name replacement was returned as the created target")
				}
			}
		})
	}
}

func TestCreateDoesNotInferPersistenceFromAnUncertainWrite(t *testing.T) {
	underlying := clientfake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	objects := &createOutcomeClient{Client: &assigningClient{Client: underlying}}
	objects.outcome = func(ctx context.Context, object client.Object) error {
		if err := underlying.Delete(ctx, object); err != nil {
			return err
		}
		return context.DeadlineExceeded
	}
	access, err := New(namespace, objects, nil, &identitypb.ServiceAccount{Namespace: namespace, Name: "app"})
	if err != nil {
		t.Fatal(err)
	}
	target, err := access.Create(t.Context(), &environmentpb.CreateEnvironmentRequest{Name: "created", Template: "pod"})
	if target != nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("unreconciled uncertain write = %v, %v", target, err)
	}
}
