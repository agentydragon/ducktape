package sandboxcontroller

import (
	"context"
	"errors"
	"testing"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	clienttesting "k8s.io/client-go/testing"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

const (
	namespace    = "test-sandboxes"
	templateName = "test-template"
	sandboxName  = "test-sandbox"
)

var (
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
			StateVolume: StateVolume{Name: "state", Claim: corev1.PersistentVolumeClaimSpec{
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
		WithStatusSubresource(&Sandbox{}, &kubevirtv1.VirtualMachineInstance{}).Build()
	return &Reconciler{Client: c, KubeVirt: true, Sealer: &fakeSealer{}, Leases: c}, c
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

func TestRunningSandboxGetsStateVolumeAndPod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)

	pvc := getPVC(t, c)
	if pvc == nil {
		t.Fatal("no state PVC")
	}
	if len(pvc.OwnerReferences) != 0 {
		t.Errorf("state PVC has owner references %v; it must outlive the Pod and survive garbage collection", pvc.OwnerReferences)
	}
	if pvc.Annotations[SandboxUIDAnnotation] != "uid-sandbox" || pvc.Labels[SandboxLabel] != sandboxName {
		t.Errorf("state PVC metadata = %v / %v", pvc.Labels, pvc.Annotations)
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
	if pvc := getPVC(t, c); pvc == nil || pvc.Annotations[SandboxUIDAnnotation] != "uid-sandbox" {
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

func TestAnotherSandboxesStateVolumeIsNotAdopted(t *testing.T) {
	stale := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: StateVolumeName(sandboxName),
		Annotations: map[string]string{SandboxUIDAnnotation: "uid-previous-sandbox"},
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

// fakeSealer answers every seal with result, or err.
type fakeSealer struct {
	result SealResult
	err    error
	calls  int
}

func (f *fakeSealer) Seal(context.Context, *Sandbox) (SealResult, error) {
	f.calls++
	return f.result, f.err
}

// sealSessions is the runner of a connected Sandbox sealing every Session, asked by one reconcile.
func sealSessions(t *testing.T, r *Reconciler) {
	t.Helper()
	sealer := r.Sealer.(*fakeSealer)
	sealer.result, sealer.err = SealResult{Sessions: 2}, nil
	reconcileOnce(t, r)
}

// connect is the Runner Gateway writing the Sandbox's connection Lease.
func connect(t *testing.T, c client.Client, runner string) {
	t.Helper()
	lease := &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: sandboxName + "-runner", Annotations: map[string]string{PodUIDAnnotation: runner},
	}}
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
	if calls := r.Sealer.(*fakeSealer).calls; calls != 0 {
		t.Errorf("asked a runner that never connected to seal %d times", calls)
	}
	if meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, SessionsSealed) {
		t.Error("a Sandbox no runner connected to was recorded as sealed")
	}
}

func TestDeletionWaitsForTheRunnerToSealAndFlush(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	connect(t, c, "uid-runner")
	sealer := r.Sealer.(*fakeSealer)
	deleteSandbox(t, c)
	for _, answer := range []struct {
		result SealResult
		err    error
	}{
		{err: errors.New("runner unreachable")},
		{result: SealResult{Sessions: 1, Unflushed: []string{"s-1"}}},
	} {
		sealer.result, sealer.err = answer.result, answer.err
		result, err := r.Reconcile(context.Background(), request)
		if err != nil || result.RequeueAfter == 0 {
			t.Fatalf("a failed or unflushed seal is asked again: %v, %v", result, err)
		}
		if getPod(t, c) == nil || meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, SessionsSealed) {
			t.Fatal("the Sandbox counted as sealed before the runner said so")
		}
	}
	sealer.result, sealer.err = SealResult{Sessions: 1}, nil
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
	r.Sealer.(*fakeSealer).err = errors.New("runner unreachable")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPod(t, c) == nil || getPVC(t, c) == nil {
		t.Fatal("deletion removed the Pod or the state PVC before the Sessions were sealed")
	}
	if reason := runningReason(t, c); reason != "Sealing" {
		t.Errorf("Running reason = %s, want Sealing", reason)
	}

	sealSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("deletion left the Pod once the Sessions were sealed")
	}
	if getPVC(t, c) == nil {
		t.Fatal("the state PVC went in the same step as the Pod, while the Pod could still be writing")
	}
	reconcileOnce(t, r)
	if getPVC(t, c) != nil {
		t.Fatal("deletion left the state PVC")
	}
	reconcileOnce(t, r)
	var sandbox Sandbox
	if err := c.Get(context.Background(), sandboxKey, &sandbox); !apierrors.IsNotFound(err) {
		t.Fatalf("Sandbox still present after its finalizer's work: %v", err)
	}
}

func TestDeletingAStoppedSandboxRunsItOnceMoreToSeal(t *testing.T) {
	r, c := newReconciler(t, false, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	connect(t, c, "uid-runner")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("a stopped Sandbox got no Pod to seal its Sessions from")
	}
	sealSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the sealing Pod outlived the seal")
	}
}

func TestDeletionLeavesAnotherSandboxesStateVolume(t *testing.T) {
	stale := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: StateVolumeName(sandboxName),
		Annotations: map[string]string{SandboxUIDAnnotation: "uid-previous-sandbox"},
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

func TestDeletionWithoutASealerWaitsForTheRunnerGateway(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	r.Sealer, r.Leases = nil, nil
	reconcileOnce(t, r)
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("the Pod went before anything sealed the Sessions")
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

func TestSuspensionWaitsForTheRunnerToSeal(t *testing.T) {
	r, c := runningWithRunner(t)
	sealer := r.Sealer.(*fakeSealer)
	sealer.err = errors.New("runner unreachable")
	setRunning(t, c, false)
	result, err := r.Reconcile(context.Background(), request)
	if err != nil || result.RequeueAfter == 0 {
		t.Fatalf("a failed seal is asked again: %v, %v", result, err)
	}
	if getPod(t, c) == nil || runningReason(t, c) != "Sealing" {
		t.Fatal("the Pod stopped before its runner sealed")
	}

	sealer.err = nil
	reconcileOnce(t, r)
	if sealed := getSandbox(t, c).Status.SealedIncarnation; sealed == nil || *sealed != "uid-pod" {
		t.Fatalf("sealed incarnation = %v, want uid-pod", sealed)
	}
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod outlived its seal")
	}
	if meta.IsStatusConditionTrue(getSandbox(t, c).Status.Conditions, SessionsSealed) {
		t.Fatal("suspension recorded the deletion-only SessionsSealed condition")
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
	if calls := r.Sealer.(*fakeSealer).calls; calls != 0 {
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

func TestDeletingASuspendedSealedSandboxAsksNoRunner(t *testing.T) {
	r, c := runningWithRunner(t)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	calls := r.Sealer.(*fakeSealer).calls
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if got := r.Sealer.(*fakeSealer).calls; got != calls || getPod(t, c) != nil {
		t.Fatalf("seals %d -> %d, Pod present %v: the sealed runner is not asked again nor run once more", calls, got, getPod(t, c) != nil)
	}
}
