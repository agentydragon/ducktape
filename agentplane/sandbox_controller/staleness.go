package sandboxcontroller

import (
	"context"
	"slices"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// How long an incarnation whose containers all run may go without its runner connecting before
	// it can be replaced; a VM guest boots inside this.
	RunnerConnectGrace = 5 * time.Minute
	// The Runner Gateway's connection Lease is `<sandbox>-runner`, and names the Pod of the
	// connection in this annotation; both are defined in runner_gateway/connection_records.py.
	runnerLeaseSuffix      = "-runner"
	runnerPodUIDAnnotation = "agentplane.allegedly.works/pod-uid"
	// On the connection Lease: the label the controller's cache selects it by.
	RunnerConnectionLabel = "agentplane.allegedly.works/runner-connection"
)

// replaceIfStale restarts an incarnation whose runner has not connected, and cannot, because it
// was rendered from a template since replaced. A template change otherwise applies at the next
// start, but such an incarnation serves nothing and nothing restarts it: its runner is the only
// thing that can seal a deleting Sandbox. It starts again from the current template; nothing is
// skipped, as the new runner seals as before. A Pod in CrashLoopBackOff is replaced at once, any
// other incarnation once all its containers have run for RunnerConnectGrace, which a VM guest's
// runner, invisible from outside, also needs.
func (r *Reconciler) replaceIfStale(ctx context.Context, sandbox *Sandbox, w workload) (workload, *unready, error) {
	incarnation := w.incarnation()
	if incarnation == nil || !incarnation.DeletionTimestamp.IsZero() {
		return w, nil, nil
	}
	if w.pod == nil && (w.vm == nil || w.vmi == nil || !vmRunning(w.vm) || !w.vm.DeletionTimestamp.IsZero()) {
		return w, nil, nil
	}
	connected, err := r.runnerConnectedOn(ctx, sandbox, incarnation)
	if err != nil || connected {
		return w, nil, err
	}
	if !crashLooping(incarnation) {
		upSince, up := containersUpSince(incarnation)
		if !up {
			return w, nil, nil
		}
		if wait := RunnerConnectGrace - r.Now().Sub(upSince); wait > 0 {
			w.recheck = wait
			return w, nil, nil
		}
	}
	template, _, err := r.template(ctx, sandbox)
	if template == nil {
		return w, nil, err
	}
	if w.pod != nil {
		current, blocked := render(sandbox, template)
		if blocked != nil || w.pod.Annotations[SpecHashAnnotation] == current.Annotations[SpecHashAnnotation] {
			return w, nil, nil
		}
		return w, nil, r.deleteIfLive(ctx, w.pod)
	}
	current, blocked := renderVM(sandbox, template)
	if blocked != nil || w.vm.Annotations[SpecHashAnnotation] == current.Annotations[SpecHashAnnotation] {
		return w, nil, nil
	}
	// With run strategy Always, KubeVirt starts the next VMI from the VM as updated here.
	w.vm.Labels, w.vm.Annotations, w.vm.Spec = current.Labels, current.Annotations, current.Spec
	if err := r.Update(ctx, w.vm); err != nil {
		return w, nil, err
	}
	return w, nil, r.deleteIfLive(ctx, w.vmi)
}

// runnerConnectedOn is whether the Runner Gateway recorded a runner connection from this Pod.
func (r *Reconciler) runnerConnectedOn(ctx context.Context, sandbox *Sandbox, pod *corev1.Pod) (bool, error) {
	var lease coordinationv1.Lease
	key := types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name + runnerLeaseSuffix}
	if err := r.Get(ctx, key, &lease); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	return lease.Annotations[runnerPodUIDAnnotation] == string(pod.UID), nil
}

func crashLooping(pod *corev1.Pod) bool {
	return slices.ContainsFunc(pod.Status.ContainerStatuses, func(status corev1.ContainerStatus) bool {
		return status.State.Waiting != nil && status.State.Waiting.Reason == "CrashLoopBackOff"
	})
}

// containersUpSince is when the last of the Pod's containers started, or false while any is not
// running or the Pod has none.
func containersUpSince(pod *corev1.Pod) (time.Time, bool) {
	var since time.Time
	for _, status := range pod.Status.ContainerStatuses {
		running := status.State.Running
		if running == nil {
			return time.Time{}, false
		}
		if running.StartedAt.Time.After(since) {
			since = running.StartedAt.Time
		}
	}
	return since, !since.IsZero()
}
