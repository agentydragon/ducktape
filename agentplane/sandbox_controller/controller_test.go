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
		name, fact, reason  string
		bootstrapped, ready bool
	}{
		{"none reported", "", "BootstrapNotReported", false, true},
		{"running", "running", "BootstrapRunning", false, false},
		{"succeeded", "succeeded", "BootstrapSucceeded", true, true},
		{"failed", "failed", "BootstrapFailed", false, false},
		{"interrupted", "interrupted", "BootstrapFailed", false, false},
		{"unknown state", "test-future-state", "BootstrapStateUnrecognised", false, false},
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
			if (ready.Status == metav1.ConditionTrue) != tc.ready || (!tc.ready && ready.Reason != tc.reason) {
				t.Errorf("Ready = %s/%s, want ready=%v with the bootstrap's reason", ready.Status, ready.Reason, tc.ready)
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
}

func (f *fakeQuiescer) Quiesce(context.Context, *Sandbox) (QuiesceResult, error) {
	f.calls++
	return f.result, f.err
}

// quiesceSessions is the runner of a connected Sandbox sealing every Session, asked by one reconcile.
func quiesceSessions(t *testing.T, r *Reconciler) {
	t.Helper()
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result, quiescer.err = QuiesceResult{Sessions: 2}, nil
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
	for _, answer := range []struct {
		result QuiesceResult
		err    error
	}{
		{err: errors.New("runner unreachable")},
		{result: QuiesceResult{Sessions: 1, Unflushed: []string{"s-1"}}},
	} {
		quiescer.result, quiescer.err = answer.result, answer.err
		result, err := r.Reconcile(context.Background(), request)
		if err != nil || result.RequeueAfter == 0 {
			t.Fatalf("a failed or unflushed seal is asked again: %v, %v", result, err)
		}
		if getPod(t, c) == nil || meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, Quiesced) {
			t.Fatal("the Sandbox counted as sealed before the runner said so")
		}
	}
	quiescer.result, quiescer.err = QuiesceResult{Sessions: 1}, nil
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod outlived a flushed seal")
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
	reconcileOnce(t, r)
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
	// The Running condition's transition time comes from the wall clock.
	r.Now = time.Now
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Quiescing: true}
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if quiescer.calls != 1 {
		t.Fatalf("asked %d times before the runner reported a stage, want 1", quiescer.calls)
	}

	publishQuiesceStage(t, c, "flushing")
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || quiescer.calls != 1 || result.RequeueAfter <= 0 || result.RequeueAfter > quiesceDeadline {
		t.Fatalf("a runner reporting a stage is left alone until the deadline: calls=%d, %v, %v", quiescer.calls, result, err)
	}

	r.Now = func() time.Time { return time.Now().Add(2 * quiesceDeadline) }
	reconcileOnce(t, r)
	if quiescer.calls != 2 {
		t.Errorf("asked %d times after the deadline, want 2", quiescer.calls)
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
	reconcileOnce(t, r)
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
