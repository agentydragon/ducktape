package sandboxes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
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
	access, err := New(namespace, objectsClient, []string{"default-policy"}, &sandboxespb.ServiceAccount{
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

func TestListGetAndTemplatesUseCRDsAndOnlyJoinCurrentPodUID(t *testing.T) {
	first := testSandbox("alpha", "sandbox-alpha", sandboxapi.OperatingModeRunning)
	first.Spec.Bootstrap = "echo boot"
	first.Spec.WakeOnSend = true
	first.Spec.IdleSuspendAfter = &metav1.Duration{Duration: 2 * time.Minute}
	deadline := metav1.NewTime(time.Date(2026, 10, 20, 1, 4, 3, 0, time.UTC))
	first.Status.IdleSuspendAt = &deadline
	activeSession := "active-session"
	first.Status.IdleSuspendBlockers = []sandboxapi.AutosuspendBlocker{
		{Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &activeSession},
		{Reason: sandboxapi.AutosuspendBlockerBootstrap},
	}
	first.Labels = map[string]string{PresetLabel: "preset-a"}
	first.Status.Incarnation = &sandboxapi.Incarnation{Name: "alpha", UID: "pod-alpha"}
	first.Status.Conditions = []metav1.Condition{{Type: sandboxapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "PodReady"}}
	missingPod := testSandbox("beta", "sandbox-beta", sandboxapi.OperatingModeRunning)
	missingPod.Status.Incarnation = &sandboxapi.Incarnation{Name: "beta", UID: "old-pod"}
	paused := testSandbox("paused", "sandbox-paused", sandboxapi.OperatingModeSuspended)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "alpha", Namespace: namespace, UID: "pod-alpha"},
		Spec:       corev1.PodSpec{NodeName: "node-a"},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "10.0.0.7"},
	}
	wrongIncarnation := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "beta", Namespace: namespace, UID: "new-pod"},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "10.0.0.8"},
	}
	templates := []client.Object{
		&sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "z-template", Namespace: namespace}},
		&sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "a-template", Namespace: namespace}},
	}
	objectsToSeed := []client.Object{first, missingPod, paused, pod, wrongIncarnation}
	objectsToSeed = append(objectsToSeed, templates...)
	access, _ := newAccess(t, objectsToSeed...)

	names, err := access.ListTemplates(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(names) != "[a-template z-template]" {
		t.Fatalf("templates = %v", names)
	}
	views, err := access.ListSandboxes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 3 || views[0].Name != "alpha" || views[1].Name != "beta" || views[2].Name != "paused" {
		t.Fatalf("sandbox views = %#v", views)
	}
	if views[0].GetPod() == nil || views[0].GetPod().GetUid() != "pod-alpha" || views[0].GetPod().GetStatus().GetFields()["podIP"].GetStringValue() != "10.0.0.7" {
		t.Fatalf("alpha Pod view = %#v", views[0].GetPod())
	}
	if views[0].GetIdleSuspendAt() == nil || !views[0].GetIdleSuspendAt().AsTime().Equal(deadline.Time) || views[1].GetIdleSuspendAt() != nil {
		t.Fatalf("Sandbox idle deadlines = %v, %v", views[0].GetIdleSuspendAt(), views[1].GetIdleSuspendAt())
	}
	blockers := views[0].GetAutosuspendBlockers()
	if len(blockers) != 2 || blockers[0].GetReason() != string(sandboxapi.AutosuspendBlockerActiveTurn) ||
		blockers[0].GetSessionId() != activeSession || blockers[1].GetReason() != string(sandboxapi.AutosuspendBlockerBootstrap) ||
		blockers[1].SessionId != nil {
		t.Fatalf("alpha autosuspend blockers = %#v", blockers)
	}
	if views[1].AutosuspendBlockers == nil || len(views[1].AutosuspendBlockers) != 0 {
		t.Fatalf("Sandbox without blockers has %#v, want an initialized empty list", views[1].AutosuspendBlockers)
	}
	if views[1].GetPod() != nil {
		t.Fatalf("stale Pod UID was joined: %#v", views[1].GetPod())
	}
	if views[0].GetPhase() != sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING ||
		views[0].GetServiceAccount().GetName() != "alpha" || views[0].GetBinding().GetBootstrap() != "echo boot" ||
		views[0].GetPreset() != "preset-a" || !views[0].GetWakeOnSend() || views[0].GetIdleSuspendAfter().AsDuration() != 2*time.Minute {
		t.Fatalf("alpha Sandbox view = %#v", views[0])
	}
	if views[2].GetPhase() != sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED {
		t.Fatalf("paused phase = %v", views[2].GetPhase())
	}
	got, err := access.Get(context.Background(), "alpha")
	if err != nil {
		t.Fatal(err)
	}
	if got.GetUid() != "sandbox-alpha" || got.GetPod().GetUid() != "pod-alpha" || got.GetIdleSuspendAt() == nil || !got.GetIdleSuspendAt().AsTime().Equal(deadline.Time) {
		t.Fatalf("Get(alpha) = %#v", got)
	}
	if _, err := access.Get(context.Background(), "absent"); !isNotFound(err) {
		t.Fatalf("Get(absent) = %v, want SandboxNotFoundError", err)
	}
}

func TestCreateAppliesDefaultsAndCallerIntentAndIsIdempotent(t *testing.T) {
	base := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(
		&policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "default-policy", Namespace: namespace}},
		&policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "extra-policy", Namespace: namespace}},
	).Build()
	objects := &assigningClient{Client: base}
	caller := &sandboxespb.ServiceAccount{Namespace: namespace, Name: "app"}
	access, err := New(namespace, objects, []string{"default-policy", "default-policy"}, caller)
	if err != nil {
		t.Fatal(err)
	}
	preset := "selected-preset"
	request := &sandboxespb.CreateSandboxRequest{
		Name: "created-sandbox", Template: "pod-template", Bootstrap: "echo < start",
		PolicySets: []string{"extra-policy"}, Preset: &preset, WakeOnSend: true,
		IdleSuspendAfter: durationpb.New(time.Minute), InboxTags: []string{"team-b", "team-a", "team-b"},
	}
	view, err := access.Create(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if view.Name != request.Name || view.Uid != "created-1" || view.GetPreset() != preset || !view.GetWakeOnSend() {
		t.Fatalf("created view = %#v", view)
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
	if stored.Labels[PresetLabel] != preset || stored.Annotations[CreateIntentAnnotation] == "" {
		t.Fatalf("stored metadata = %#v", stored.ObjectMeta)
	}
	wantIntent := `{"caller":{"name":"app","namespace":"test-sandboxes"},"request":{"bootstrap":"echo < start","idle_suspend_after":"60s","inbox_tags":["team-b","team-a","team-b"],"name":"created-sandbox","policy_sets":["extra-policy"],"preset":"selected-preset","template":"pod-template","wake_on_send":true}}`
	if got := stored.Annotations[CreateIntentAnnotation]; got != wantIntent {
		t.Fatalf("create intent = %s, want %s", got, wantIntent)
	}
	retry, err := access.Create(context.Background(), request)
	if err != nil || retry.GetUid() != view.GetUid() {
		t.Fatalf("same Create retry = %#v, %v", retry, err)
	}
	otherCaller, err := New(namespace, objects, nil, &sandboxespb.ServiceAccount{Namespace: namespace, Name: "other"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := otherCaller.Create(context.Background(), request); !isConflict(err) {
		t.Fatalf("different caller adopted name: %v", err)
	}
}

func TestCreateRejectsUnknownPolicySetsAndInvalidRequests(t *testing.T) {
	access, _ := newAccess(t)
	_, err := access.Create(context.Background(), &sandboxespb.CreateSandboxRequest{Name: "unknown-policy", Template: "pod"})
	var unknown *UnknownPolicySetError
	if !errors.As(err, &unknown) || fmt.Sprint(unknown.Names) != "[default-policy]" {
		t.Fatalf("Create with unknown default = %v", err)
	}
	emptyPreset := ""
	nonASCIIPreset := "ašb"
	for _, request := range []*sandboxespb.CreateSandboxRequest{
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

func TestSuspendResumeAndPolicyUpdatesKeepUIDFenceAndWakeGeneration(t *testing.T) {
	resource := testSandbox("paused", "sandbox-uid", sandboxapi.OperatingModeSuspended)
	resource.Spec.Running = false
	resource.Spec.WakeGeneration = 4
	idle := sandboxapi.SuspendedByIdle
	generation := int64(4)
	resource.Status.SuspendedBy = &idle
	resource.Status.SuspendedAtWakeGeneration = &generation
	access, objects := newAccess(t, resource)

	if err := access.Resume(context.Background(), "paused"); err != nil {
		t.Fatal(err)
	}
	if err := access.Resume(context.Background(), "paused"); err != nil {
		t.Fatal(err)
	}
	stored := readSandbox(t, objects, "paused")
	if !stored.Spec.Running || stored.Spec.WakeGeneration != 5 {
		t.Fatalf("resume state = running %v, generation %d", stored.Spec.Running, stored.Spec.WakeGeneration)
	}
	if err := access.Suspend(context.Background(), "paused"); err != nil {
		t.Fatal(err)
	}
	stored = readSandbox(t, objects, "paused")
	if stored.Spec.Running || stored.Spec.WakeGeneration != 5 {
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

func TestResumeRefusesQuiescingAndMutationsRefuseTeardown(t *testing.T) {
	resource := testSandbox("paused", "uid", sandboxapi.OperatingModeSuspended)
	resource.Status.StopStage = sandboxapi.StopStageQuiescing
	access, _ := newAccess(t, resource)
	var quiescing *SandboxQuiescingError
	if err := access.Resume(context.Background(), "paused"); !errors.As(err, &quiescing) {
		t.Fatalf("resume during quiesce = %v", err)
	}
	deleting := metav1.NewTime(time.Now())
	resource = testSandbox("deleting", "uid-delete", sandboxapi.OperatingModeSuspended)
	resource.DeletionTimestamp = &deleting
	resource.Finalizers = []string{"test-finalizer"}
	access, _ = newAccess(t, resource)
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
	if err := access.Delete(context.Background(), "running"); !errors.As(err, &runningError) {
		t.Fatalf("delete running Sandbox = %v", err)
	}
	var modeError *SandboxModeUnknownError
	if err := access.Delete(context.Background(), "unknown"); !errors.As(err, &modeError) {
		t.Fatalf("delete unknown-mode Sandbox = %v", err)
	}
	if err := access.Delete(context.Background(), "paused"); err != nil {
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
	access, err := New(namespace, conflicts, nil, &sandboxespb.ServiceAccount{Namespace: namespace, Name: "app"})
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
	access, err = New(namespace, replacing, nil, &sandboxespb.ServiceAccount{Namespace: namespace, Name: "app"})
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

func TestSandboxPodViewIncludesEmptyStatus(t *testing.T) {
	view, err := sandboxPodView(&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "empty", Namespace: namespace}})
	if err != nil {
		t.Fatal(err)
	}
	if view.Status == nil {
		t.Fatal("empty Kubernetes Pod status lost its Struct presence")
	}
}

func TestViewUsesOnlyTheReportedPodIncarnation(t *testing.T) {
	resource := testSandbox("worker", "sandbox-uid", sandboxapi.OperatingModeRunning)
	resource.Status.Incarnation = &sandboxapi.Incarnation{Name: "worker", UID: "current-pod"}
	current := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "worker", Namespace: namespace, UID: "current-pod"}}
	stale := current.DeepCopy()
	stale.UID = "stale-pod"

	view, err := View(resource, current)
	if err != nil || view.GetPod() == nil || view.GetPod().GetUid() != "current-pod" {
		t.Fatalf("View(current Pod) = %#v, %v", view, err)
	}
	view, err = View(resource, stale)
	if err != nil || view.GetPod() != nil {
		t.Fatalf("View(stale Pod) = %#v, %v; stale incarnation must not be joined", view, err)
	}
}
