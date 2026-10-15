package sandboxcontroller

import (
	"context"
	"errors"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	runtimeschema "k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	clienttesting "k8s.io/client-go/testing"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

const (
	namespace    = "test-sandboxes"
	templateName = "test-template"
	sandboxName  = "test-sandbox"
)

var (
	testNow    = time.Date(2026, 10, 13, 12, 0, 0, 0, time.UTC)
	sandboxKey = client.ObjectKey{Namespace: namespace, Name: sandboxName}
	request    = reconcile.Request{NamespacedName: sandboxKey}
	pvcKey     = client.ObjectKey{Namespace: namespace, Name: StateVolumeName(sandboxName)}
)

func testTemplate(image string) *SandboxTemplate {
	return &SandboxTemplate{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: templateName},
		Spec: SandboxTemplateSpec{
			Pod: &corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app.kubernetes.io/name": "test-runner"}},
				Spec: corev1.PodSpec{Containers: []corev1.Container{{
					Name:         "runner",
					Image:        image,
					VolumeMounts: []corev1.VolumeMount{{Name: "state", MountPath: "/state"}},
				}}},
			},
			StateVolume: StateVolume{Name: "state", Layout: VolumeLayout{Type: DirectoryTree}, Claim: corev1.PersistentVolumeClaimSpec{
				AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
				Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("1Gi"),
				}},
			}},
		},
	}
}

func newReconciler(t *testing.T, running bool, objects ...client.Object) (*Reconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := policycontroller.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	sandbox := &Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: sandboxName, UID: "uid-sandbox"},
		Spec:       SandboxSpec{Template: templateName, Running: running},
	}
	// A tracker without managed fields: the fake client's default one cannot convert KubeVirt's
	// uint64 fields.
	tracker := clienttesting.NewObjectTracker(scheme, serializer.NewCodecFactory(scheme).UniversalDecoder())
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjectTracker(tracker).WithObjects(append(objects, sandbox)...).
		WithStatusSubresource(&Sandbox{}, &kubevirtv1.VirtualMachineInstance{}).
		WithInterceptorFuncs(interceptor.Funcs{Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			// The API server assigns a PVC UID on creation; the fake tracker does not.
			if _, claim := obj.(*corev1.PersistentVolumeClaim); claim && obj.GetUID() == "" {
				obj.SetUID(types.UID("uid-" + obj.GetName()))
			}
			return c.Create(ctx, obj, opts...)
		}}).Build()
	return &Reconciler{Client: c, KubeVirt: true, Quiescer: &fakeQuiescer{}, Leases: c, Now: func() time.Time { return testNow }}, c
}

func reconcileOnce(t *testing.T, r *Reconciler) {
	t.Helper()
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
}

func getSandbox(t *testing.T, c client.Client) *Sandbox {
	t.Helper()
	var sandbox Sandbox
	if err := c.Get(context.Background(), sandboxKey, &sandbox); err != nil {
		t.Fatal(err)
	}
	return &sandbox
}

func setRunning(t *testing.T, c client.Client, running bool) {
	t.Helper()
	sandbox := getSandbox(t, c)
	sandbox.Spec.Running = running
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
}

func getPod(t *testing.T, c client.Client) *corev1.Pod {
	t.Helper()
	var pod corev1.Pod
	if err := c.Get(context.Background(), sandboxKey, &pod); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &pod
}

func getPVC(t *testing.T, c client.Client) *corev1.PersistentVolumeClaim {
	t.Helper()
	var pvc corev1.PersistentVolumeClaim
	if err := c.Get(context.Background(), pvcKey, &pvc); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &pvc
}

func runningReason(t *testing.T, c client.Client) string {
	t.Helper()
	condition := meta.FindStatusCondition(getSandbox(t, c).Status.Conditions, Running)
	if condition == nil {
		t.Fatal("no Running condition")
	}
	return condition.Reason
}

func getReadyCondition(t *testing.T, c client.Client) *metav1.Condition {
	t.Helper()
	condition := meta.FindStatusCondition(getSandbox(t, c).Status.Conditions, Ready)
	if condition == nil {
		t.Fatal("no Ready condition")
	}
	return condition
}

func setPodReady(t *testing.T, c client.Client, pod *corev1.Pod, ready corev1.ConditionStatus) {
	t.Helper()
	pod.Status.Phase = corev1.PodRunning
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: ready}}
	if err := c.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
}

func getCondition(t *testing.T, c client.Client, conditionType string) *metav1.Condition {
	t.Helper()
	condition := meta.FindStatusCondition(getSandbox(t, c).Status.Conditions, conditionType)
	if condition == nil {
		t.Fatalf("no %s condition", conditionType)
	}
	return condition
}

func TestSandboxIsReadyWhileItsPodIsReadyAndItsRunnerConnected(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.UID = "uid-pod"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	setPodReady(t, c, getPod(t, c), corev1.ConditionFalse)
	connect(t, c, "uid-pod")
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "PodNotReady" {
		t.Errorf("Ready = %s/%s with a running Pod that is not Ready, want False/PodNotReady", got.Status, got.Reason)
	}
	setPodReady(t, c, getPod(t, c), corev1.ConditionTrue)
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionTrue {
		t.Errorf("Ready = %s/%s with a Ready Pod and a connected runner, want True", got.Status, got.Reason)
	}
	if got := getCondition(t, c, WorkloadReady); got.Status != metav1.ConditionTrue {
		t.Errorf("WorkloadReady = %s/%s, want True", got.Status, got.Reason)
	}
}

func TestReadyWaitsForTheBootstrapAndStopsOnItsFailure(t *testing.T) {
	for _, tc := range []struct {
		name, fact, reason string
		bootstrapped       bool
		ready              metav1.ConditionStatus
	}{
		{"none reported", "", "BootstrapNotReported", false, metav1.ConditionUnknown},
		{"running", "running", "BootstrapRunning", false, metav1.ConditionFalse},
		{"succeeded", "succeeded", "BootstrapSucceeded", true, metav1.ConditionTrue},
		{"failed", "failed", "BootstrapFailed", false, metav1.ConditionFalse},
		{"interrupted", "interrupted", "BootstrapFailed", false, metav1.ConditionFalse},
		{"unknown state", "test-future-state", "BootstrapStateUnrecognised", false, metav1.ConditionFalse},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, c := newReconciler(t, true, testTemplate("test-image:v1"))
			setBootstrap(t, c, "echo test-bootstrap")
			reconcileOnce(t, r)
			pod := getPod(t, c)
			pod.UID = "uid-pod"
			if err := c.Update(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			setPodReady(t, c, getPod(t, c), corev1.ConditionTrue)
			connect(t, c, "uid-pod")
			if tc.fact != "" {
				var lease coordinationv1.Lease
				if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + "-runner"}, &lease); err != nil {
					t.Fatal(err)
				}
				lease.Annotations[BootstrapAnnotation] = tc.fact
				if err := c.Update(context.Background(), &lease); err != nil {
					t.Fatal(err)
				}
			}
			reconcileOnce(t, r)
			bootstrapped, ready := getCondition(t, c, Bootstrapped), getReadyCondition(t, c)
			if bootstrapped.Reason != tc.reason || (bootstrapped.Status == metav1.ConditionTrue) != tc.bootstrapped {
				t.Errorf("Bootstrapped = %s/%s, want %v/%s", bootstrapped.Status, bootstrapped.Reason, tc.bootstrapped, tc.reason)
			}
			if ready.Status != tc.ready || (tc.ready != metav1.ConditionTrue && ready.Reason != tc.reason) {
				t.Errorf("Ready = %s/%s, want %s with the bootstrap's reason", ready.Status, ready.Reason, tc.ready)
			}
		})
	}
}

func TestSandboxWithoutABootstrapIsBootstrapped(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	if got := getCondition(t, c, Bootstrapped); got.Status != metav1.ConditionTrue || got.Reason != "NoBootstrap" {
		t.Errorf("Bootstrapped = %s/%s, want True/NoBootstrap", got.Status, got.Reason)
	}
}

func TestSandboxWhoseRunnerNeverConnectedIsNotReady(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	setPodReady(t, c, getPod(t, c), corev1.ConditionTrue)
	result, err := r.Reconcile(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if got := getCondition(t, c, WorkloadReady); got.Status != metav1.ConditionTrue {
		t.Errorf("WorkloadReady = %s/%s with a Ready Pod, want True", got.Status, got.Reason)
	}
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "NeverConnected" {
		t.Errorf("Ready = %s/%s, want False/NeverConnected", got.Status, got.Reason)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("requeue = %s, want none: the Lease watch enqueues when the runner connects", result.RequeueAfter)
	}
}

func TestRunnerOfAnEarlierIncarnationDoesNotMakeTheSandboxReady(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.UID = "uid-pod"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	setPodReady(t, c, getPod(t, c), corev1.ConditionTrue)
	connect(t, c, "uid-earlier-pod")
	reconcileOnce(t, r)
	if got := getCondition(t, c, RunnerConnected); got.Status != metav1.ConditionFalse || got.Reason != "AwaitingRunner" {
		t.Errorf("RunnerConnected = %s/%s, want False/AwaitingRunner", got.Status, got.Reason)
	}
}

func TestConnectionDropIsToleratedUntilTheLeaseLapsesPastTheGrace(t *testing.T) {
	r, c := runningWithRunner(t)
	setPodReady(t, c, getPod(t, c), corev1.ConditionTrue)
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionTrue {
		t.Fatalf("Ready = %s/%s with a connected runner, want True", got.Status, got.Reason)
	}
	// The Lease's 30 s duration has passed, as in a Runner Gateway rollout, but not the grace.
	r.Now = func() time.Time { return testNow.Add(45 * time.Second) }
	result, err := r.Reconcile(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionTrue {
		t.Errorf("Ready = %s/%s inside the grace, want True", got.Status, got.Reason)
	}
	if want := 30*time.Second + runnerDisconnectGrace - 45*time.Second; result.RequeueAfter != want {
		t.Errorf("requeue = %s, want %s, the moment the connection would lapse", result.RequeueAfter, want)
	}
	r.Now = func() time.Time { return testNow.Add(2 * time.Minute) }
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "ConnectionLost" {
		t.Errorf("Ready = %s/%s past the grace, want False/ConnectionLost", got.Status, got.Reason)
	}
	if pod := getPod(t, c); !pod.DeletionTimestamp.IsZero() {
		t.Error("the Pod is deleted because its connection lapsed; nothing is replaced for it")
	}
}

func TestSandboxThatIsNotRunningIsNotReadyForTheSameReason(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	got := getReadyCondition(t, c)
	if got.Status != metav1.ConditionFalse || got.Reason != runningReason(t, c) {
		t.Errorf("Ready = %s/%s before the Pod runs, want False with the Running reason %s", got.Status, got.Reason, runningReason(t, c))
	}
}

func TestRunningSandboxGetsStateVolumeAndPod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)

	pvc := getPVC(t, c)
	if pvc == nil {
		t.Fatal("no state PVC")
	}
	owner := metav1.GetControllerOf(pvc)
	if owner == nil || owner.UID != "uid-sandbox" || (owner.BlockOwnerDeletion != nil && *owner.BlockOwnerDeletion) {
		t.Errorf("state PVC owner = %v; want the Sandbox, without blocking its deletion so foreground deletion cannot remove the state before the seal", owner)
	}
	if pvc.Labels[SandboxLabel] != sandboxName {
		t.Errorf("state PVC labels = %v", pvc.Labels)
	}
	if got := pvc.Spec.Resources.Requests[corev1.ResourceStorage]; got.String() != "1Gi" {
		t.Errorf("state PVC request = %s, want the template's 1Gi", got.String())
	}

	pod := getPod(t, c)
	if pod == nil {
		t.Fatal("no Pod")
	}
	if !metav1.IsControlledBy(pod, getSandbox(t, c)) {
		t.Errorf("Pod owner references = %v", pod.OwnerReferences)
	}
	if pod.Labels[SandboxLabel] != sandboxName || pod.Labels["app.kubernetes.io/name"] != "test-runner" {
		t.Errorf("Pod labels = %v", pod.Labels)
	}
	var claim string
	for _, v := range pod.Spec.Volumes {
		if v.Name == "state" && v.PersistentVolumeClaim != nil {
			claim = v.PersistentVolumeClaim.ClaimName
		}
	}
	if claim != StateVolumeName(sandboxName) {
		t.Errorf("Pod state volume claims %q, want %q", claim, StateVolumeName(sandboxName))
	}
	if status := getSandbox(t, c).Status; status.Incarnation == nil || status.Incarnation.Name != sandboxName {
		t.Errorf("status incarnation = %v", status.Incarnation)
	}
	if reason := runningReason(t, c); reason != "Starting" {
		t.Errorf("Running reason = %s, want Starting before the Pod runs", reason)
	}
}

func TestUnschedulablePodConditionReachesSandboxReady(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.Status.Phase = corev1.PodPending
	pod.Status.Conditions = []corev1.PodCondition{{
		Type: corev1.PodScheduled, Status: corev1.ConditionFalse,
		Reason: "Unschedulable", Message: "0/2 nodes are available: PVC is not bound",
	}}
	if err := c.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}

	reconcileOnce(t, r)
	if got := getCondition(t, c, Running); got.Reason != "Unschedulable" || got.Message != "0/2 nodes are available: PVC is not bound" {
		t.Errorf("Running = %s: %q, want the PodScheduled failure", got.Reason, got.Message)
	}
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "Unschedulable" || got.Message != "0/2 nodes are available: PVC is not bound" {
		t.Errorf("Ready = %s/%s: %q, want the PodScheduled failure", got.Status, got.Reason, got.Message)
	}
}

func TestStoppedSandboxKeepsStateVolumeWithoutPod(t *testing.T) {
	r, c := newReconciler(t, false, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	if getPVC(t, c) == nil {
		t.Fatal("a stopped Sandbox still holds its state PVC")
	}
	if getPod(t, c) != nil {
		t.Fatal("a stopped Sandbox has a Pod")
	}
	if reason := runningReason(t, c); reason != "Stopped" {
		t.Errorf("Running reason = %s, want Stopped", reason)
	}
}

func TestStopDeletesPodAndKeepsStateVolume(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("stopping left the Pod")
	}
	if getPVC(t, c) == nil {
		t.Fatal("stopping deleted the state PVC")
	}
	reconcileOnce(t, r)
	if reason := runningReason(t, c); reason != "Stopped" {
		t.Errorf("Running reason = %s, want Stopped", reason)
	}
	if incarnation := getSandbox(t, c).Status.Incarnation; incarnation != nil {
		t.Errorf("a stopped Sandbox reports incarnation %v", incarnation)
	}
}

func TestStartResolvesTheTemplateAsItIsThen(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)

	template := testTemplate("test-image:v2")
	var current SandboxTemplate
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(template), &current); err != nil {
		t.Fatal(err)
	}
	current.Spec = template.Spec
	if err := c.Update(context.Background(), &current); err != nil {
		t.Fatal(err)
	}
	setRunning(t, c, true)
	reconcileOnce(t, r)
	pod := getPod(t, c)
	if pod == nil || pod.Spec.Containers[0].Image != "test-image:v2" {
		t.Fatalf("restarted Pod = %v, want the updated template's image", pod)
	}
}

func TestPodGoneOnItsOwnIsReplaced(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	first := getPod(t, c)
	if err := c.Delete(context.Background(), first); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("a running Sandbox whose Pod went away got no new one")
	}
	if pvc := getPVC(t, c); pvc == nil || !metav1.IsControlledBy(pvc, getSandbox(t, c)) {
		t.Fatal("the state PVC did not survive the Pod")
	}
}

func TestEndedPodIsDeleted(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.Status.Phase = corev1.PodFailed
	if err := c.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if reason := runningReason(t, c); reason != "PodEnded" {
		t.Errorf("Running reason = %s, want PodEnded", reason)
	}
	if getPod(t, c) != nil {
		t.Fatal("the ended Pod was not deleted to make way for the next incarnation")
	}
}

func TestSuspendedThenResumedPodRunsTheCurrentTemplateAndRecordsItsSpec(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	before := getPod(t, c)
	replaceTemplate(t, c, testTemplate("test-image:v2"))
	setRunning(t, c, false)
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)
	after := getPod(t, c)
	if after == nil || after.Spec.Containers[0].Image != "test-image:v2" {
		t.Fatalf("the resumed Pod does not run the current template's image: %v", after)
	}
	if after.Annotations[RenderedSpecAnnotation] == before.Annotations[RenderedSpecAnnotation] {
		t.Error("the rendered spec was not recorded afresh for the new incarnation")
	}
}

func TestMissingTemplateBlocksStart(t *testing.T) {
	r, c := newReconciler(t, true)
	reconcileOnce(t, r)
	if getPod(t, c) != nil || getPVC(t, c) != nil {
		t.Fatal("created resources without a template")
	}
	if reason := runningReason(t, c); reason != "TemplateNotFound" {
		t.Errorf("Running reason = %s, want TemplateNotFound", reason)
	}
}

func TestTemplateDefiningTheStateVolumeIsRefused(t *testing.T) {
	template := testTemplate("test-image:v1")
	template.Spec.Pod.Spec.Volumes = []corev1.Volume{{Name: "state", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("started a Pod whose state volume the template replaced")
	}
	if reason := runningReason(t, c); reason != "TemplateInvalid" {
		t.Errorf("Running reason = %s, want TemplateInvalid", reason)
	}
}

func TestStateVolumeTheSandboxDoesNotOwnIsNotUsed(t *testing.T) {
	stale := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: StateVolumeName(sandboxName),
	}}
	r, c := newReconciler(t, true, testTemplate("test-image:v1"), stale)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("started a Pod on a previous Sandbox's state")
	}
	if reason := runningReason(t, c); reason != "StateVolumeConflict" {
		t.Errorf("Running reason = %s, want StateVolumeConflict", reason)
	}
}

// fakeQuiescer answers every seal with result, or err.
type fakeQuiescer struct {
	result QuiesceResult
	err    error
	calls  int
	before func()
}

func (f *fakeQuiescer) Quiesce(context.Context, *Sandbox) (QuiesceResult, error) {
	f.calls++
	if f.before != nil {
		f.before()
	}
	return f.result, f.err
}

// quiesceSessions is the runner publishing its completed stage after a connected Sandbox was asked to seal.
func quiesceSessions(t *testing.T, r *Reconciler) {
	t.Helper()
	publishQuiesceStage(t, r.Client, "quiesced")
	reconcileOnce(t, r)
}

// connect is the Runner Gateway writing the Sandbox's connection Lease.
func connect(t *testing.T, c client.Client, runner string) {
	t.Helper()
	renewed, duration := metav1.NewMicroTime(testNow), int32(30)
	lease := &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: sandboxName + "-runner", Annotations: map[string]string{PodUIDAnnotation: runner}},
		Spec:       coordinationv1.LeaseSpec{RenewTime: &renewed, LeaseDurationSeconds: &duration},
	}
	if err := c.Create(context.Background(), lease); err != nil {
		t.Fatal(err)
	}
}

func TestDeletionOfASandboxNoRunnerConnectedToSealsNothing(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod outlived a Sandbox with nothing to seal")
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Errorf("asked a runner that never connected to seal %d times", calls)
	}
	if meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, Quiesced) {
		t.Error("a Sandbox no runner connected to was recorded as sealed")
	}
}

func TestDeletionWaitsForTheRunnerToSealAndFlush(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	connect(t, c, "uid-runner")
	quiescer := r.Quiescer.(*fakeQuiescer)
	deleteSandbox(t, c)
	quiescer.err = errors.New("runner unreachable")
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || result.RequeueAfter != quiesceDeadline || quiescer.calls != 1 {
		t.Fatalf("a failed quiesce schedules one bounded retry: calls=%d, %v, %v", quiescer.calls, result, err)
	}
	if getPod(t, c) == nil || meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, Quiesced) {
		t.Fatal("the Sandbox counted as sealed before the runner said so")
	}
	quiescer.err = nil
	quiescer.result = QuiesceResult{Sessions: 1, Unflushed: []string{"s-1"}}
	result, err = r.Reconcile(context.Background(), request)
	if err != nil || result.RequeueAfter != quiesceDeadline || quiescer.calls != 1 {
		t.Fatalf("a repeated reconcile is held to the saved deadline: calls=%d, %v, %v", quiescer.calls, result, err)
	}
	// The Lease fact completes promptly without another Quiesce call.
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod outlived a quiesced runner")
	}
}

func deleteSandbox(t *testing.T, c client.Client) {
	t.Helper()
	if err := c.Delete(context.Background(), getSandbox(t, c)); err != nil {
		t.Fatal(err)
	}
}

func TestDeletionSealsThenRemovesPodThenStateVolume(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	connect(t, c, "uid-runner")
	r.Quiescer.(*fakeQuiescer).err = errors.New("runner unreachable")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPod(t, c) == nil || getPVC(t, c) == nil {
		t.Fatal("deletion removed the Pod or the state PVC before the Sessions were sealed")
	}
	if reason := runningReason(t, c); reason != "Quiescing" {
		t.Errorf("Running reason = %s, want Quiescing", reason)
	}

	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("deletion left the Pod once the Sessions were sealed")
	}
	if getPVC(t, c) == nil {
		t.Fatal("the state PVC went in the same step as the Pod, while the Pod could still be writing")
	}
	reconcileOnce(t, r)
	var sandbox Sandbox
	if err := c.Get(context.Background(), sandboxKey, &sandbox); !apierrors.IsNotFound(err) {
		t.Fatalf("Sandbox still present after its finalizer's work: %v", err)
	}
}

func TestDeletingAStoppedSandboxRunsItOnceMoreToQuiesce(t *testing.T) {
	r, c := newReconciler(t, false, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	connect(t, c, "uid-runner")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("a stopped Sandbox got no Pod to seal its Sessions from")
	}
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the sealing Pod outlived the seal")
	}
}

func TestDeletionLeavesAStateVolumeTheSandboxDoesNotOwn(t *testing.T) {
	stale := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: StateVolumeName(sandboxName),
	}}
	r, c := newReconciler(t, false, testTemplate("test-image:v1"), stale)
	reconcileOnce(t, r)
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPVC(t, c) == nil {
		t.Fatal("deleting a Sandbox deleted a state PVC it never created")
	}
	// It never had a state PVC, so never a runner or a Session to seal.
	if getPod(t, c) != nil {
		t.Error("a Sandbox that never ran was started to seal")
	}
	var sandbox Sandbox
	if err := c.Get(context.Background(), sandboxKey, &sandbox); !apierrors.IsNotFound(err) {
		t.Fatalf("a Sandbox with nothing to seal or delete is still present: %v", err)
	}
}

// runningWithRunner is a running Sandbox whose Pod has a UID and whose runner connected from it.
func runningWithRunner(t *testing.T) (*Reconciler, client.Client) {
	t.Helper()
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.UID = "uid-pod"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	connect(t, c, "uid-pod")
	reconcileOnce(t, r)
	return r, c
}

func TestSuspensionWaitsForTheRunnerToQuiesce(t *testing.T) {
	r, c := runningWithRunner(t)
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.err = errors.New("runner unreachable")
	setRunning(t, c, false)
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || result.RequeueAfter == 0 {
		t.Fatalf("a failed seal is asked again: %v, %v", result, err)
	}
	if getPod(t, c) == nil || runningReason(t, c) != "Quiescing" {
		t.Fatal("the Pod stopped before its runner sealed")
	}

	quiescer.err = nil
	quiesceSessions(t, r)
	if sealed := getSandbox(t, c).Status.QuiescedIncarnation; sealed == nil || *sealed != "uid-pod" {
		t.Fatalf("sealed incarnation = %v, want uid-pod", sealed)
	}
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod outlived its seal")
	}
	if meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, Quiesced) {
		t.Fatal("suspension recorded the deletion-only Quiesced condition")
	}
}

// publishQuiesceStage is the Runner Gateway recording the runner's quiesce stage on its Lease.
func publishQuiesceStage(t *testing.T, c client.Client, stage string) {
	t.Helper()
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + "-runner"}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Annotations[QuiesceStageAnnotation] = stage
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
}

func publishBootstrap(t *testing.T, c client.Client, state string) {
	t.Helper()
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + "-runner"}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Annotations[BootstrapAnnotation] = state
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
}

func TestSuspensionWaitsForARunningBootstrapAndThenQuiesces(t *testing.T) {
	for _, finished := range []string{"succeeded", "failed"} {
		t.Run(finished, func(t *testing.T) {
			r, c := runningWithRunner(t)
			publishBootstrap(t, c, "running")
			setRunning(t, c, false)
			reconcileOnce(t, r)
			if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 || getPod(t, c) == nil || runningReason(t, c) != "SuspendPending" {
				t.Fatalf("quiesce calls = %d, Running reason = %s, want 0 and SuspendPending with the Pod kept", calls, runningReason(t, c))
			}
			if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "SuspendPending" {
				t.Errorf("Ready = %s/%s, want False/SuspendPending", got.Status, got.Reason)
			}
			publishBootstrap(t, c, finished)
			reconcileOnce(t, r)
			if runningReason(t, c) != "Quiescing" {
				t.Errorf("Running reason = %s once the bootstrap %s, want Quiescing", runningReason(t, c), finished)
			}
		})
	}
}

func TestSuspensionWaitsForARunningSetup(t *testing.T) {
	r, c := runningWithRunner(t)
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + "-runner"}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Annotations[SetupsRunningAnnotation] = "true"
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 || runningReason(t, c) != "SuspendPending" {
		t.Fatalf("quiesce calls = %d, Running reason = %s, want 0 and SuspendPending", calls, runningReason(t, c))
	}
	delete(lease.Annotations, SetupsRunningAnnotation)
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if runningReason(t, c) != "Quiescing" {
		t.Errorf("Running reason = %s once the setup ended, want Quiescing", runningReason(t, c))
	}
}

func TestDeletionDoesNotWaitForARunningBootstrap(t *testing.T) {
	r, c := runningWithRunner(t)
	publishBootstrap(t, c, "running")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls == 0 {
		t.Error("a delete waited for the bootstrap instead of quiescing")
	}
}

func TestAQuiescedStageSealsTheSandboxWithoutAskingTheRunner(t *testing.T) {
	r, c := runningWithRunner(t)
	publishQuiesceStage(t, c, "quiesced")
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if sealed := getSandbox(t, c).Status.QuiescedIncarnation; sealed == nil || *sealed != "uid-pod" {
		t.Fatalf("sealed incarnation = %v, want uid-pod", sealed)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Errorf("asked a runner that reported itself quiesced %d times", calls)
	}
}

func TestAQuiescingRunnerIsAskedAgainOnlyAtTheDeadline(t *testing.T) {
	r, c := runningWithRunner(t)
	now := testNow
	r.Now = func() time.Time { return now }
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Quiescing: true}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if quiescer.calls != 1 {
		t.Fatalf("asked %d times before the runner reported a stage, want 1", quiescer.calls)
	}
	if attempt := getSandbox(t, c).Status.QuiesceAttempt; attempt == nil || attempt.RunnerPodUID != "uid-pod" || !attempt.AttemptedAt.Time.Equal(now) {
		t.Fatalf("persisted quiesce attempt = %v, want uid-pod at %s", attempt, now)
	}

	publishQuiesceStage(t, c, "flushing")
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || quiescer.calls != 1 || result.RequeueAfter <= 0 || result.RequeueAfter > quiesceDeadline {
		t.Fatalf("a runner reporting a stage is left alone until the deadline: calls=%d, %v, %v", quiescer.calls, result, err)
	}

	now = now.Add(quiesceDeadline)
	reconcileOnce(t, r)
	if quiescer.calls != 2 {
		t.Errorf("asked %d times after the deadline, want 2", quiescer.calls)
	}
}

func TestFailedQuiesceWaitsAcrossNoStageEventsAndControllerRestart(t *testing.T) {
	r, c := runningWithRunner(t)
	now := testNow
	r.Now = func() time.Time { return now }
	oldTransition := metav1.NewTime(now.Add(-5 * quiesceDeadline))
	sandbox := getSandbox(t, c)
	running := meta.FindStatusCondition(sandbox.Status.Conditions, Running)
	if running == nil || running.Status != metav1.ConditionFalse {
		t.Fatalf("Running before quiesce = %v, want an existing False condition", running)
	}
	running.LastTransitionTime = oldTransition
	if err := c.Status().Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	if got := meta.FindStatusCondition(getSandbox(t, c).Status.Conditions, Running).LastTransitionTime; !got.Equal(&oldTransition) {
		t.Fatalf("test setup kept LastTransitionTime %s, want %s", got, oldTransition)
	}
	setRunning(t, c, false)

	first := r.Quiescer.(*fakeQuiescer)
	first.err = errors.New("ingress unavailable")
	first.before = func() {
		attempt := getSandbox(t, c).Status.QuiesceAttempt
		if attempt == nil || attempt.RunnerPodUID != "uid-pod" || !attempt.AttemptedAt.Time.Equal(now) {
			t.Fatalf("Quiesce ran before its attempt was persisted: %v", attempt)
		}
	}
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || first.calls != 1 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("the unavailable ingress gets one bounded attempt: calls=%d, %v, %v", first.calls, result, err)
	}
	if got := meta.FindStatusCondition(getSandbox(t, c).Status.Conditions, Running).LastTransitionTime; !got.Equal(&oldTransition) {
		t.Fatalf("a False-to-False reason change reset Running.LastTransitionTime: got %s, want %s", got, oldTransition)
	}

	// A controller restart and the status event from the attempt write both see the persisted deadline.
	afterRestart := &fakeQuiescer{result: QuiesceResult{Unflushed: []string{"session-1"}}}
	restarted := &Reconciler{Client: c, KubeVirt: true, Quiescer: afterRestart, Leases: c, Now: func() time.Time { return now }}
	// A resume followed by another suspend continues this same runner's quiesce operation; it does not
	// make a new request intent that reopens the per-incarnation retry budget.
	setRunning(t, c, true)
	result, err = restarted.Reconcile(context.Background(), request)
	if err != nil || afterRestart.calls != 0 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("resume bypassed the runner's saved deadline: calls=%d, %v, %v", afterRestart.calls, result, err)
	}
	setRunning(t, c, false)
	result, err = restarted.Reconcile(context.Background(), request)
	if err != nil || afterRestart.calls != 0 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("a repeated suspend reopened the runner's retry budget: calls=%d, %v, %v", afterRestart.calls, result, err)
	}
	for _, event := range []string{"status update", "controller restart"} {
		result, err = restarted.Reconcile(context.Background(), request)
		if err != nil || afterRestart.calls != 0 || result.RequeueAfter != quiesceDeadline {
			t.Fatalf("%s bypassed the saved deadline: calls=%d, %v, %v", event, afterRestart.calls, result, err)
		}
	}

	// Even a Lease stage event cannot turn the retry into a per-event RPC loop.
	publishQuiesceStage(t, c, "flushing")
	result, err = restarted.Reconcile(context.Background(), request)
	if err != nil || afterRestart.calls != 0 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("the flushing event bypassed the saved deadline: calls=%d, %v, %v", afterRestart.calls, result, err)
	}

	// The existing deadline recovers a lost stage event without a periodic poll.
	now = now.Add(quiesceDeadline)
	result, err = restarted.Reconcile(context.Background(), request)
	if err != nil || afterRestart.calls != 1 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("the deadline did not recover the unfinished seal: calls=%d, %v, %v", afterRestart.calls, result, err)
	}

	// A final Lease fact completes the suspend immediately, despite the newly recorded retry time.
	publishQuiesceStage(t, c, "quiesced")
	reconcileOnce(t, restarted)
	if attempt := getSandbox(t, c).Status.QuiesceAttempt; attempt != nil {
		t.Fatalf("completed quiesce retained retry state: %v", attempt)
	}
	if sealed := getSandbox(t, c).Status.QuiescedIncarnation; sealed == nil || *sealed != "uid-pod" {
		t.Fatalf("sealed incarnation = %v, want uid-pod", sealed)
	}
	reconcileOnce(t, restarted)
	if getPod(t, c) != nil {
		t.Fatal("the suspended Pod was removed before its runner reported quiesced")
	}
}

func TestFailedQuiesceAttemptStatusWriteDoesNotCallRunner(t *testing.T) {
	r, c := runningWithRunner(t)
	setRunning(t, c, false)
	conflict := apierrors.NewConflict(runtimeschema.GroupResource{Group: "agentplane.allegedly.works", Resource: "sandboxes"}, sandboxName, errors.New("stale resource version"))
	r.Client = interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, name string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if sandbox, ok := obj.(*Sandbox); ok && sandbox.Status.QuiesceAttempt != nil {
				return conflict
			}
			return c.SubResource(name).Update(ctx, obj, opts...)
		},
	})

	_, err := r.Reconcile(context.Background(), request)
	if !apierrors.IsConflict(err) {
		t.Fatalf("attempt status write error = %v, want conflict", err)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("called runner %d times without a persisted attempt", calls)
	}
	if getSandbox(t, c).Status.QuiesceAttempt != nil || getPod(t, c) == nil || getPVC(t, c) == nil {
		t.Fatal("failed status write changed retry state or released deletion-protected state")
	}
}

func TestQuiesceAttemptDoesNotThrottleANewRunnerIncarnationDuringDeletion(t *testing.T) {
	r, c := runningWithRunner(t)
	now := testNow
	r.Now = func() time.Time { return now }
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.err = errors.New("gateway unavailable")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if quiescer.calls != 1 || getPod(t, c) == nil || getPVC(t, c) == nil {
		t.Fatal("deletion did not keep the Pod and state PVC behind the failed quiesce")
	}

	// A restarted runner gets a new Pod UID and must be asked immediately, even within the old deadline.
	pod := getPod(t, c)
	pod.UID = "uid-next"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + "-runner"}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Annotations[PodUIDAnnotation] = "uid-next"
	delete(lease.Annotations, QuiesceStageAnnotation)
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if quiescer.calls != 2 {
		t.Fatalf("new runner was held to the old incarnation's deadline: calls=%d", quiescer.calls)
	}
	if attempt := getSandbox(t, c).Status.QuiesceAttempt; attempt == nil || attempt.RunnerPodUID != "uid-next" {
		t.Fatalf("new runner attempt = %v, want uid-next", attempt)
	}
	if getPod(t, c) == nil || getPVC(t, c) == nil {
		t.Fatal("deletion removed state before the new runner completed quiesce")
	}
}

func TestSuspensionOfAnIncarnationNoRunnerConnectedFromSealsNothing(t *testing.T) {
	r, c := runningWithRunner(t)
	pod := getPod(t, c)
	pod.UID = "uid-later-pod"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod was held for a seal it had none to make")
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Errorf("sealed %d times for an incarnation whose runner never connected", calls)
	}
}

func TestResumingASealedIncarnationReplacesIt(t *testing.T) {
	r, c := runningWithRunner(t)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("a resumed Sandbox kept the Pod whose runner sealed its Sessions")
	}
}

func TestResumingWhileTheRunnerQuiescesFinishesTheQuiesceFirst(t *testing.T) {
	r, c := runningWithRunner(t)
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Unflushed: []string{"session-1"}}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)
	if meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, Ready) {
		t.Fatal("a Sandbox whose runner refuses every Session read Ready while it was quiescing")
	}

	quiescer.result = QuiesceResult{Sessions: 1}
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if pod := getPod(t, c); pod != nil && string(pod.UID) == "uid-pod" {
		t.Fatal("the resumed Sandbox kept the incarnation whose runner quiesced")
	}
}

func TestARunnerThatHasNotFinishedQuiescingIsNotTakenForQuiesced(t *testing.T) {
	r, c := runningWithRunner(t)
	// The Running condition's transition time comes from the wall clock.
	r.Now = time.Now
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Quiescing: true}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if sealed := getSandbox(t, c).Status.QuiescedIncarnation; sealed != nil || getPod(t, c) == nil {
		t.Fatal("a quiesce that is still draining was recorded as final and the Pod removed")
	}
}

func TestDeletingASuspendedSealedSandboxAsksNoRunner(t *testing.T) {
	r, c := runningWithRunner(t)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	calls := r.Quiescer.(*fakeQuiescer).calls
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if got := r.Quiescer.(*fakeQuiescer).calls; got != calls || getPod(t, c) != nil {
		t.Fatalf("seals %d -> %d, Pod present %v: the sealed runner is not asked again nor run once more", calls, got, getPod(t, c) != nil)
	}
}

func TestAnIdleSuspendedSandboxStopsUntilItsWakeGenerationIsBumped(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	if got := getSandbox(t, c).Status.OperatingMode; got != OperatingModeRunning {
		t.Fatalf("operating mode = %s, want Running", got)
	}
	suspended := getSandbox(t, c)
	idle, generation := SuspendedByIdle, suspended.Spec.WakeGeneration
	suspended.Status.SuspendedBy, suspended.Status.SuspendedAtWakeGeneration = &idle, &generation
	if err := c.Status().Update(context.Background(), suspended); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("an idle-suspended Sandbox kept its Pod")
	}
	if got := getSandbox(t, c).Status.OperatingMode; got != OperatingModeSuspended {
		t.Errorf("operating mode = %s, want Suspended", got)
	}
	woken := getSandbox(t, c)
	woken.Spec.WakeGeneration++
	if err := c.Update(context.Background(), woken); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("a bumped wake generation did not start the Sandbox")
	}
	if got := getSandbox(t, c).Status.OperatingMode; got != OperatingModeRunning {
		t.Errorf("operating mode = %s, want Running", got)
	}
}

func TestAStoppedSandboxIsSuspended(t *testing.T) {
	r, c := newReconciler(t, false, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	if got := getSandbox(t, c).Status.OperatingMode; got != OperatingModeSuspended {
		t.Errorf("operating mode = %s, want Suspended", got)
	}
}
