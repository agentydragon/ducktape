package sandboxcontroller

import (
	"context"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var started = time.Date(2026, 10, 13, 5, 0, 0, 0, time.UTC)

func atMinutes(minutes int) func() time.Time {
	return func() time.Time { return started.Add(time.Duration(minutes) * time.Minute) }
}

// containersRunning marks every container of the Pod as running since `started`.
func containersRunning(t *testing.T, c client.Client, pod *corev1.Pod) {
	t.Helper()
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  "runner",
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(started)}},
	}}
	if err := c.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
}

func runnerConnected(t *testing.T, c client.Client, pod *corev1.Pod) {
	t.Helper()
	lease := &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: sandboxName + runnerLeaseSuffix,
		Annotations: map[string]string{runnerPodUIDAnnotation: string(pod.UID)},
	}}
	if err := c.Create(context.Background(), lease); err != nil {
		t.Fatal(err)
	}
}

func startedPod(t *testing.T, r *Reconciler, c client.Client) *corev1.Pod {
	t.Helper()
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.UID = "uid-pod"
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	containersRunning(t, c, pod)
	return pod
}

func TestRunningPodThatNeverConnectedIsRecreatedFromTheCurrentTemplateAfterTheGrace(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	startedPod(t, r, c)
	replaceTemplate(t, c, testTemplate("test-image:v2"))

	r.Now = atMinutes(1)
	result, err := r.Reconcile(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if getPod(t, c) == nil || result.RequeueAfter <= 0 || result.RequeueAfter > RunnerConnectGrace {
		t.Fatalf("within the grace the Pod must stay and be looked at again: %v, requeue %v", getPod(t, c), result.RequeueAfter)
	}

	r.Now = atMinutes(10)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the Pod of the replaced template, never connected, was kept past the grace")
	}
	reconcileOnce(t, r)
	if pod := getPod(t, c); pod == nil || pod.Spec.Containers[0].Image != "test-image:v2" {
		t.Fatalf("the next Pod does not run the current template's image: %v", pod)
	}
}

func TestPodWhoseRunnerConnectedIsKeptAfterATemplateChange(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	pod := startedPod(t, r, c)
	runnerConnected(t, c, pod)
	crashLoop(t, c)
	replaceTemplate(t, c, testTemplate("test-image:v2"))
	r.Now = atMinutes(10)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("replaced a Pod whose runner is connected")
	}
}

func TestPodThatNeverConnectedIsKeptWhenTheTemplateRendersItAgain(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	startedPod(t, r, c)
	r.Now = atMinutes(10)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("replaced a Pod with a Pod of the same spec")
	}
}

func TestVMGuestThatNeverConnectedIsRestartedFromTheCurrentTemplateAfterTheGrace(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	vmi, launcher := startVMI(t, c, kubevirtv1.Running)
	containersRunning(t, c, launcher)
	replaceTemplate(t, c, testVMTemplate("test-guest:v2"))

	r.Now = atMinutes(10)
	reconcileOnce(t, r)
	var remaining kubevirtv1.VirtualMachineInstance
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(vmi), &remaining); err == nil && remaining.DeletionTimestamp.IsZero() {
		t.Fatal("the VMI of the replaced template, never connected, was kept past the grace")
	}
	if image := getVM(t, c).Spec.Template.Spec.Volumes[0].ContainerDisk.Image; image != "test-guest:v2" {
		t.Fatalf("the VM was not updated to the current template for the next VMI: %s", image)
	}
}
