package sandboxcontroller

import (
	"context"
	"errors"
	"slices"
	"testing"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
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
	pod, blocked := RenderPod(sandbox, template, requester.Issued{ID: "0123456789abcdef0123456789abcdef", SecretName: runtimeCredentialTestSecret})
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
	for _, tc := range []struct {
		name   string
		state  FirecrackerState
		err    error
		reason string
	}{
		{name: "running", state: FirecrackerState{PodUID: string(pod.UID), Phase: "running"}, reason: "FirecrackerPending"},
		{name: "suspending", state: FirecrackerState{PodUID: string(pod.UID), Phase: "suspending"}, reason: "FirecrackerPending"},
		{name: "unavailable", err: errors.New("proxy unavailable"), reason: "FirecrackerStatusUnavailable"},
		{name: "wrong pod", state: FirecrackerState{PodUID: "wrong", Phase: "suspended"}, reason: "FirecrackerStatusMismatch"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			status.state, status.err = tc.state, tc.err
			blocked, err := r.convergeFirecracker(context.Background(), sandbox, &current, true)
			if err != nil || blocked == nil || blocked.reason != tc.reason {
				t.Fatalf("pending suspension = %v, %v", blocked, err)
			}
			if err := c.Get(context.Background(), client.ObjectKeyFromObject(pod), &current); err != nil {
				t.Fatal(err)
			}
			if current.Annotations[FirecrackerRunningAnnotation] != "false" {
				t.Fatal("resume cancelled an unacknowledged suspension")
			}
		})
	}
	status.state = FirecrackerState{PodUID: string(pod.UID), Phase: "suspended"}
	status.err = nil
	blocked, err = r.convergeFirecracker(context.Background(), sandbox, &current, true)
	if err != nil || blocked == nil || blocked.reason != "FirecrackerPending" {
		t.Fatalf("resume intent = %v, %v", blocked, err)
	}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(pod), &current); err != nil {
		t.Fatal(err)
	}
	if current.Annotations[FirecrackerRunningAnnotation] != "true" || current.UID != pod.UID {
		t.Fatal("acknowledged suspension did not resume the same Pod")
	}
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
		issueCredential(t, c, sandboxKey)
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
		t.Fatalf("suspend quiesced the RAM-preserved Environment Runtime %d times", calls)
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
		t.Fatalf("suspend/resume quiesced Environment Runtime %d times", calls)
	}
	credential := *getSandbox(t, c).Status.RunnerCredential
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
	if *stopped.Status.RunnerCredential != credential || runningReason(t, c) != "FirecrackerLauncherLost" {
		t.Fatalf("lost launcher changed credential or escaped recovery hold: %s", runningReason(t, c))
	}
}

func TestFirecrackerDeletionWaitsForFreshRuntimeStateAfterResume(t *testing.T) {
	r, c, now, status, _ := firecrackerIdleController(t)
	pod := getPod(t, c)
	firecrackerToResumingAfterWake(t, r, c, now, status)
	reconcileResult(t, r)
	status.state.Phase = "running"
	reconcileResult(t, r)
	if snapshot := r.States.Snapshot(getSandbox(t, c), string(pod.UID)); snapshot.BootID != "" {
		t.Fatalf("Runtime snapshot survived Firecracker checkpoint/resume: %+v", snapshot)
	}
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("quiesced before a post-resume Runtime snapshot: %d calls", calls)
	}
	if current := getPod(t, c); current == nil || current.UID != pod.UID {
		t.Fatal("deletion removed the retained launcher while waiting for fresh Runtime state")
	}
	publishSnapshot(t, r, string(pod.UID), EnvironmentRuntimeFacts{})
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 1 {
		t.Fatalf("post-resume Runtime state made %d Quiesce calls, want one", calls)
	}
	if getPod(t, c) == nil {
		t.Fatal("launcher deleted before recording the seal")
	}
}

func TestFirecrackerDeletionRetainsUnsealedStateWhenLauncherIsLost(t *testing.T) {
	r, c := runningWithRuntime(t)
	sandbox := getSandbox(t, c)
	sandbox.Status.Volumes.State.Layout = sandboxapi.VolumeLayout{Type: sandboxapi.RawDiskImage, Filesystem: "ext4"}
	if err := c.Status().Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(context.Background(), getPod(t, c)); err != nil {
		t.Fatal(err)
	}
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if runningReason(t, c) != "FirecrackerLauncherLost" || getPVC(t, c) == nil {
		t.Fatal("unsealed launcher loss discarded its retained state")
	}
}

func TestFirecrackerIdleRequiresFreshRuntimeSnapshotAfterResume(t *testing.T) {
	r, c, now, status, _ := firecrackerIdleController(t)
	pod := getPod(t, c)
	firecrackerToResumingAfterWake(t, r, c, now, status)
	reconcileResult(t, r)
	status.state.Phase = "running"
	reconcileResult(t, r)
	if snapshot := r.States.Snapshot(getSandbox(t, c), string(pod.UID)); snapshot.BootID != "" {
		t.Fatalf("Runtime snapshot survived Firecracker checkpoint/resume: %+v", snapshot)
	}
	publishIdleSince(t, r, &runtimeIdleSince)
	check, err := r.suspendIfIdle(context.Background(), getSandbox(t, c), workload{pod: getPod(t, c)})
	if err != nil || check.recheck != idleSuspendAfter {
		t.Fatalf("a resumed guest's first fresh state did not restart the idle timer: %+v, %v", check, err)
	}
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 {
		t.Fatalf("Firecracker idle called Quiesce %d times", calls)
	}
}

func TestFirecrackerLauncherLossAfterStatusConflictRetainsState(t *testing.T) {
	for _, deleting := range []bool{false, true} {
		name := "running"
		if deleting {
			name = "deleting"
		}
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			r, c := newReconciler(t, true, firecrackerTemplate())
			concurrentWrites := 0
			r.Credentials = testRequester(t, interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
				Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
					if err := c.Update(ctx, obj, opts...); err != nil {
						return err
					}
					if record, ok := obj.(*workloadapi.WorkloadCredential); ok && record.Spec.Pod != nil {
						// A concurrent Sandbox writer races the final status update after the
						// launcher is created and its credential is bound, before publication.
						sandbox := &sandboxapi.Sandbox{}
						if err := c.Get(ctx, sandboxKey, sandbox); err != nil {
							return err
						}
						ensureAnnotations(sandbox)
						sandbox.Annotations["test.example/concurrent-writer"] = "changed"
						concurrentWrites++
						return c.Update(ctx, sandbox)
					}
					return nil
				},
			}))
			conflicted := false
			for range 5 {
				issueCredential(t, c, sandboxKey)
				_, err := r.Reconcile(ctx, request)
				if apierrors.IsConflict(err) {
					conflicted = true
					break
				}
				if err != nil {
					t.Fatal(err)
				}
			}
			if !conflicted || concurrentWrites != 1 {
				t.Fatalf("launcher status did not conflict: conflicted=%v writes=%d", conflicted, concurrentWrites)
			}
			pod := getPod(t, c)
			if pod == nil || !isFirecrackerPod(pod) {
				t.Fatal("no Firecracker launcher was created")
			}
			if getSandbox(t, c).Status.Incarnation != nil {
				t.Fatal("launcher was published before the failed status update")
			}
			pvc := getPVC(t, c)
			if pvc == nil {
				t.Fatal("no retained state PVC")
			}
			if err := c.Delete(ctx, pod); err != nil {
				t.Fatal(err)
			}
			if deleting {
				if err := c.Delete(ctx, getSandbox(t, c)); err != nil {
					t.Fatal(err)
				}
			}
			restarted := &Reconciler{Client: c, Objects: c, KubeVirt: true, Credentials: testRequester(t, c), States: NewRuntimeStates(nil, r.Now), Now: r.Now, Quiescer: &fakeQuiescer{}}
			if _, err := restarted.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
			if reason := runningReason(t, c); reason != "FirecrackerLauncherLost" {
				t.Fatalf("missing launcher reason = %s", reason)
			}
			if getPod(t, c) != nil {
				t.Fatal("missing launcher was replaced")
			}
			retained := getPVC(t, c)
			if retained == nil || retained.UID != pvc.UID || !retained.DeletionTimestamp.IsZero() {
				t.Fatal("lost launcher released its retained state PVC")
			}
			if !slices.Contains(getSandbox(t, c).Finalizers, StateVolumeFinalizer) {
				t.Fatal("lost launcher released the state volume finalizer")
			}
		})
	}
}
