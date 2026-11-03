package sandboxcontroller

import (
	"context"
	"errors"
	"slices"
	"strconv"
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type testFirecrackerStatus struct {
	state FirecrackerState
	err   error
}

func (s *testFirecrackerStatus) Read(context.Context, *corev1.Pod) (FirecrackerState, error) {
	return s.state, s.err
}

func firecrackerTemplate() *sandboxapi.SandboxTemplate {
	template := testTemplate("launcher:v1")
	template.Spec.Pod.Spec.Containers[0].VolumeMounts = nil
	template.Spec.Firecracker = &sandboxapi.FirecrackerTemplate{
		Pod:       *template.Spec.Pod,
		BootImage: "boot:v1", RunnerImage: "runner:v1", VCPUCount: 2, MemoryMiB: 2048,
	}
	template.Spec.Pod = nil
	template.Spec.StateVolume.Layout = sandboxapi.VolumeLayout{Type: sandboxapi.RawDiskImage, Filesystem: "ext4"}
	template.Spec.WorkspaceVolume = &sandboxapi.WorkspaceVolume{
		Name: "workspace", Claim: *template.Spec.StateVolume.Claim.DeepCopy(),
		Layout: sandboxapi.VolumeLayout{Type: sandboxapi.RawDiskImage, Filesystem: "ext4"},
	}
	return template
}

func TestFirecrackerRenderAndLayout(t *testing.T) {
	template := firecrackerTemplate()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: sandboxName, Namespace: namespace, UID: "uid-sandbox"}}
	pod, blocked := render(sandbox, template, "0123456789abcdef0123456789abcdef")
	if blocked != nil {
		t.Fatalf("render blocked: %v", blocked)
	}
	if pod.Labels[EnvironmentBackendLabel] != "firecracker" || pod.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("launcher identity/intent missing: labels=%v annotations=%v", pod.Labels, pod.Annotations)
	}
	if !slices.ContainsFunc(pod.Spec.Volumes, func(v corev1.Volume) bool { return v.Image != nil && v.Image.Reference == "boot:v1" }) ||
		!slices.ContainsFunc(pod.Spec.Volumes, func(v corev1.Volume) bool { return v.Image != nil && v.Image.Reference == "runner:v1" }) {
		t.Fatal("launcher lacks both immutable image volumes")
	}
	c := pod.Spec.Containers[0]
	for _, path := range []string{FirecrackerIntentMountPath, FirecrackerStateMountPath, FirecrackerBootMountPath, FirecrackerRunnerMountPath, LaunchMountPath, EnvironmentRuntimeTLSMountPath} {
		if !slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool { return m.MountPath == path }) {
			t.Errorf("missing mount %s", path)
		}
	}
	if validateStateVolume(template) != nil {
		t.Fatal("valid Firecracker state layout rejected")
	}
	template.Spec.StateVolume.Layout = sandboxapi.VolumeLayout{Type: sandboxapi.DirectoryTree}
	if validateStateVolume(template) == nil {
		t.Fatal("DirectoryTree Firecracker state accepted")
	}
}

func TestFirecrackerSuspendResumeKeepsPodAndChecksSupervisorUID(t *testing.T) {
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: sandboxName, Namespace: namespace, UID: "pod-one", Labels: map[string]string{EnvironmentBackendLabel: "firecracker"}, Annotations: map[string]string{FirecrackerRunningAnnotation: "true"}}}
	r, c := newReconciler(t, true, pod)
	status := &testFirecrackerStatus{state: FirecrackerState{PodUID: "wrong", Phase: "suspended"}}
	r.FirecrackerStatus = status
	sandbox := getSandbox(t, c)
	blocked, err := r.convergeFirecracker(context.Background(), sandbox, pod, false)
	if err != nil || blocked == nil || blocked.reason != "FirecrackerPending" {
		t.Fatalf("stop intent = %v, %v", blocked, err)
	}
	var current corev1.Pod
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(pod), &current); err != nil {
		t.Fatal(err)
	}
	if current.UID != pod.UID || current.Annotations[FirecrackerRunningAnnotation] != "false" {
		t.Fatalf("launcher replaced or intent not set: %+v", current)
	}
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, false)
	if err != nil || blocked == nil || blocked.reason != "FirecrackerStatusMismatch" {
		t.Fatalf("foreign status accepted: %v, %v", blocked, err)
	}
	status.state.PodUID = string(pod.UID)
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, false)
	if err != nil || blocked == nil || blocked.stage != sandboxapi.StopStageStopped {
		t.Fatalf("matching checkpoint not accepted: %v, %v", blocked, err)
	}
	status.err = errors.New("proxy unavailable")
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, true)
	if err != nil || blocked == nil || blocked.reason != "FirecrackerPending" {
		t.Fatalf("resume intent = %v, %v", blocked, err)
	}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(pod), &current); err != nil {
		t.Fatal(err)
	}
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, true)
	if err != nil || blocked == nil || blocked.reason != "FirecrackerStatusUnavailable" {
		t.Fatalf("proxy outage accepted as running: %v, %v", blocked, err)
	}
	status.err = nil
	status.state.Phase = "running"
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, true)
	if err != nil || blocked != nil {
		t.Fatalf("resume remained blocked: %v, %v", blocked, err)
	}
}

func TestFirecrackerReconcileSuspendsWithoutQuiescingOrDeletingPod(t *testing.T) {
	r, c := newReconciler(t, true, firecrackerTemplate())
	status := &testFirecrackerStatus{state: FirecrackerState{PodUID: "uid-pod", Phase: "running"}}
	r.FirecrackerStatus = status
	for attempt := 0; attempt < 8 && getPod(t, c) == nil; attempt++ {
		if _, err := r.Reconcile(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	pod := getPod(t, c)
	if pod == nil {
		t.Fatal("Firecracker launcher was not created")
	}
	uid := pod.UID
	sandbox := getSandbox(t, c)
	sandbox.Spec.Running = false
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if got := getPod(t, c); got == nil || got.UID != uid || got.Annotations[FirecrackerRunningAnnotation] != "false" {
		t.Fatalf("suspend replaced launcher or failed to request checkpoint: %v", got)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("suspend quiesced the RAM-preserved runner %d times", calls)
	}
	status.state.Phase = "suspended"
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if stage := getSandbox(t, c).Status.StopStage; stage != sandboxapi.StopStageStopped {
		t.Fatalf("stop stage = %s, want Stopped after checkpoint", stage)
	}
	sandbox = getSandbox(t, c)
	sandbox.Spec.Running = true
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	status.state.Phase = "running"
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if got := getPod(t, c); got == nil || got.UID != uid || got.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("resume replaced launcher or failed to wake it: %v", got)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("suspend/resume quiesced runner %d times", calls)
	}
	credential := getSandbox(t, c).Status.RunnerCredential.ID
	current := getPod(t, c)
	if err := c.Delete(context.Background(), current); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if getPod(t, c) != nil {
		t.Fatal("lost launcher was cold booted on retained state")
	}
	stopped := getSandbox(t, c)
	if stopped.Status.RunnerCredential.ID != credential || runningReason(t, c) != "FirecrackerLauncherLost" {
		t.Fatalf("lost launcher changed credential or escaped recovery hold: %s", runningReason(t, c))
	}
}

func TestFirecrackerIdleIgnoresPreSuspendLeaseUntilNewConnectionEpoch(t *testing.T) {
	r, c, _ := idleRunner(t, durationPtr(idleSuspendAfter))
	status := &testFirecrackerStatus{state: FirecrackerState{PodUID: "uid-pod", Phase: "running"}}
	r.FirecrackerStatus = status
	pod := getPod(t, c)
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + sandboxlease.EnvironmentRuntimeLeaseSuffix}, &lease); err != nil {
		t.Fatal(err)
	}
	epoch := leaseTransitions(&lease)
	pod.Labels[EnvironmentBackendLabel] = "firecracker"
	pod.Annotations[FirecrackerRunningAnnotation] = "true"
	pod.Annotations[FirecrackerSuspendEpochAnnotation] = strconv.FormatInt(int64(epoch), 10)
	if err := c.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	check, err := r.suspendIfIdle(context.Background(), getSandbox(t, c), workload{pod: pod})
	if err != nil || check.recheck != 0 || check.suspendAt != nil {
		t.Fatalf("stale idle Lease scheduled suspension: %+v, %v", check, err)
	}
	next := epoch + 1
	lease.Spec.LeaseTransitions = &next
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	check, err = r.suspendIfIdle(context.Background(), getSandbox(t, c), workload{pod: pod})
	if err != nil || check.recheck != idleSuspendAfter {
		t.Fatalf("fresh epoch did not restart the full idle timer: %+v, %v", check, err)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("Firecracker idle called Quiesce %d times", calls)
	}
}
