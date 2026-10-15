package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

const (
	Running = "Running"
	// True while the incarnation Pod, a VM's virt-launcher Pod included, is Ready: the workload's own
	// readiness, which says nothing of whether the runner has connected to the Runner Gateway.
	WorkloadReady = "WorkloadReady"
	// True while the incarnation's runner holds a live connection to the Runner Gateway, read from the
	// Gateway's Lease for the Sandbox.
	RunnerConnected = "RunnerConnected"
	// True once the Sandbox's bootstrap has run to success, or when it has none; a Sandbox-lifetime
	// fact the connected runner republishes at each connect. False with `BootstrapFailed` when the
	// bootstrap failed or was interrupted, which is terminal. Unknown while the runner reports none,
	// which does not hold `Ready`. Absent from a controller not configured with Leases.
	Bootstrapped = "Bootstrapped"
	// True when the Sandbox can take work: WorkloadReady, RunnerConnected and no False Bootstrapped. The
	// lifecycle phase is separate.
	Ready = "Ready"
	// Set True on a deleting Sandbox once its runner has sealed every Session and the ingress has
	// acknowledged each seal; nothing of the Sandbox is deleted before, unless no runner ever connected.
	Quiesced = "Quiesced"
	// On the Pod or the VirtualMachine and its VMI and virt-launcher Pod, the state PVC, the
	// ServiceAccount and the PolicyBinding: the Sandbox they belong to.
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// On the Runner Gateway's Lease for each Sandbox: selects the connection Leases among the namespace's.
	ConnectionLabel = "agentplane.allegedly.works/runner-connection"
	// On the Runner Gateway's Lease for a Sandbox: the Pod UID of the runner that last connected.
	PodUIDAnnotation = "agentplane.allegedly.works/pod-uid"
	// Seals a deleted Sandbox's Sessions, then deletes its Pod or VM; the state PVC goes by garbage
	// collection once the Sandbox does.
	StateVolumeFinalizer = "agentplane.allegedly.works/state-volume"
	// On the Pod or VirtualMachine: the JSON of the spec rendered from the template when it was
	// created or last started, so it can be diffed against what the template renders now. The
	// The integration app reads this annotation to show the rendered workload differences.
	RenderedSpecAnnotation = "agentplane.allegedly.works/rendered-spec"

	templateIndex = "spec.template"
	// How long a failed or unfinished quiesce is left alone before the idempotent Quiesce call is
	// repeated. Runner facts enqueue the Sandbox on progress; this deadline recovers a lost event or a
	// stuck runner without polling.
	quiesceDeadline = time.Minute
	// How soon a connection whose Lease has lapsed is looked at again: a renewal changes nothing the
	// Lease watch enqueues on.
	runnerLapsedPoll = 5 * time.Second
	// How long past its Lease's expiry a runner still counts as connected. A Runner Gateway rollout
	// drops every connection at once and the runners reconnect to the new replicas.
	runnerDisconnectGrace = time.Minute
)

// Reconciler converges one Sandbox at a time: its state PVC, its Pod or VirtualMachine, and its
// Running condition.
//
// +kubebuilder:object:generate=false
type Reconciler struct {
	client.Client
	// Whether the cluster serves KubeVirt's API. Without it VM Sandboxes stay unready and Pod
	// Sandboxes are unaffected.
	KubeVirt bool
	// Seals a Sandbox's Sessions through its runner before it stops or is deleted.
	Quiescer Quiescer
	// Reads the Lease the Runner Gateway writes for each connection, and, set up with a manager, watches
	// them: a new connection, or a runner reconnecting, enqueues its Sandbox.
	Leases client.Reader
	// The current time, for the liveness of a connection Lease.
	Now func() time.Time
}

// StateVolumeName is the Sandbox's state PVC.
func StateVolumeName(sandbox string) string { return sandbox + "-state" }

// WorkspaceVolumeName is the Sandbox's retained workspace PVC.
func WorkspaceVolumeName(sandbox string) string { return sandbox + "-workspace" }

// unready is why the Sandbox cannot run; its reason and message go on the Running condition.
//
// +kubebuilder:object:generate=false
type unready struct {
	reason, message string
	// Nonzero where no watch event will end the wait.
	retryAfter time.Duration
}

func (u *unready) retryAfterOrZero() time.Duration {
	if u == nil {
		return 0
	}
	return u.retryAfter
}

// workload is what runs the Sandbox, or ran it: its Pod, or its VirtualMachine with the VMI and
// virt-launcher Pod of the VM's current run. A start deletes the other kind's first, so only one
// kind is there unless someone else made it.
//
// +kubebuilder:object:generate=false
type workload struct {
	pod      *corev1.Pod
	vm       *kubevirtv1.VirtualMachine
	vmi      *kubevirtv1.VirtualMachineInstance
	launcher *corev1.Pod
}

// incarnation is the Pod the runner runs in, if any.
func (w workload) incarnation() *corev1.Pod {
	if w.pod != nil {
		return w.pod
	}
	return w.launcher
}

func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (ctrl.Result, error) {
	var sandbox Sandbox
	if err := r.Get(ctx, req.NamespacedName, &sandbox); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	w, err := r.workload(ctx, &sandbox)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !sandbox.DeletionTimestamp.IsZero() {
		if !controllerutil.ContainsFinalizer(&sandbox, StateVolumeFinalizer) {
			return ctrl.Result{}, nil
		}
		w, blocked, err := r.finalize(ctx, &sandbox, w)
		if err != nil || blocked == nil {
			return ctrl.Result{}, err
		}
		return r.updateStatus(ctx, &sandbox, w, blocked)
	}
	if controllerutil.AddFinalizer(&sandbox, StateVolumeFinalizer) {
		if err := r.Update(ctx, &sandbox); err != nil {
			return ctrl.Result{}, err
		}
	}
	blocked, err := r.ensureStateVolume(ctx, &sandbox)
	if err != nil {
		return ctrl.Result{}, err
	}
	gate, err := r.ensureIdentity(ctx, &sandbox)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := r.ensureControlService(ctx, &sandbox); err != nil {
		return ctrl.Result{}, err
	}
	if launchGate, err := r.ensureLaunch(ctx, &sandbox); err != nil {
		return ctrl.Result{}, err
	} else if gate == nil {
		gate = launchGate
	}
	if blocked == nil && (!sandbox.running() || sandbox.quiescing()) {
		if blocked, err = r.quiesceBeforeStop(ctx, &sandbox, w); err != nil {
			return ctrl.Result{}, err
		}
	}
	if blocked == nil {
		if w, blocked, err = r.converge(ctx, &sandbox, w, sandbox.running(), gate); err != nil {
			return ctrl.Result{}, err
		}
	}
	return r.updateStatus(ctx, &sandbox, w, blocked)
}

func (r *Reconciler) workload(ctx context.Context, sandbox *Sandbox) (workload, error) {
	var w workload
	var err error
	if w.pod, err = r.pod(ctx, sandbox); err != nil {
		return w, err
	}
	if !r.KubeVirt {
		return w, nil
	}
	if w.vm, err = r.vm(ctx, sandbox); err != nil {
		return w, err
	}
	if w.vmi, err = r.vmi(ctx, sandbox); err != nil {
		return w, err
	}
	if w.launcher, err = r.launcher(ctx, sandbox, w.vmi); err != nil {
		return w, err
	}
	return w, nil
}

// pod is the Sandbox's Pod, or nil; a same-name Pod it does not control is not its.
func (r *Reconciler) pod(ctx context.Context, sandbox *Sandbox) (*corev1.Pod, error) {
	var pod corev1.Pod
	if err := r.Get(ctx, client.ObjectKeyFromObject(sandbox), &pod); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(&pod, sandbox) {
		return nil, nil
	}
	return &pod, nil
}

// finalize seals the Sandbox's Sessions, then deletes the Pod, then the state PVC once no Pod can
// be writing to it, then lets the Sandbox go. Each step's watch event runs the next. It returns why
// the Sandbox is still there, or nil once the finalizer is removed.
func (r *Reconciler) finalize(ctx context.Context, sandbox *Sandbox, w workload) (workload, *unready, error) {
	pvc, err := r.stateVolume(ctx, sandbox)
	if err != nil {
		return w, nil, err
	}
	own := pvc != nil && metav1.IsControlledBy(pvc, sandbox)
	// Without a state PVC of its own the Sandbox never had a runner, so it has no Session to seal;
	// nor has one whose runner never connected, which is not sealed, only has nothing to seal.
	needsQuiesce := own && !sandbox.sessionsQuiesced()
	var runner string
	if needsQuiesce {
		var err error
		if runner, err = r.unquiescedRunner(ctx, sandbox); err != nil {
			return w, nil, err
		}
		needsQuiesce = runner != ""
	}
	if needsQuiesce {
		// Only a running runner seals, so a stopped Sandbox runs once more; the Runner Gateway
		// refuses every new Session and command for a deleting Sandbox.
		w, blocked, err := r.converge(ctx, sandbox, w, true, nil)
		if err != nil || blocked != nil {
			return w, blocked, err
		}
		blocked, err = r.quiesce(ctx, sandbox, runner)
		if err != nil || blocked == nil {
			return w, blocked, err
		}
		if blocked.retryAfter == 0 {
			meta.SetStatusCondition(&sandbox.Status.Conditions, metav1.Condition{
				Type: Quiesced, Status: metav1.ConditionTrue, Reason: "Quiesced", Message: blocked.message, ObservedGeneration: sandbox.Generation,
			})
		}
		return w, blocked, nil
	}
	if w.pod != nil {
		if err := r.deleteIfLive(ctx, w.pod); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "Deleting", message: fmt.Sprintf("Pod %s is being deleted", w.pod.Name)}, nil
	}
	if w.vm != nil {
		// The VM's DataVolumes, the VM-local workspace among them, go with it.
		if err := r.deleteIfLive(ctx, w.vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "Deleting", message: fmt.Sprintf("VM %s is being deleted", w.vm.Name)}, nil
	}
	if w.vmi != nil {
		// KubeVirt removes a VMI only once its virt-launcher Pod is gone, so no guest still writes to
		// the state PVC after this.
		return w, &unready{reason: "Deleting", message: fmt.Sprintf("VMI %s is being deleted", w.vmi.Name)}, nil
	}
	controllerutil.RemoveFinalizer(sandbox, StateVolumeFinalizer)
	return w, nil, r.Update(ctx, sandbox)
}

// connectedRunner is the Pod UID of the runner that last connected for the Sandbox, which the Runner
// Gateway records on the Sandbox's Lease, or "" if none ever did.
func (r *Reconciler) connectedRunner(ctx context.Context, sandbox *Sandbox) (string, error) {
	lease, err := r.runnerLease(ctx, sandbox)
	return lease.Annotations[PodUIDAnnotation], err
}

// runnerLease reads the Lease the Runner Gateway writes for the Sandbox's runner connection; it is
// empty if no runner ever connected.
func (r *Reconciler) runnerLease(ctx context.Context, sandbox *Sandbox) (coordinationv1.Lease, error) {
	var lease coordinationv1.Lease
	err := r.Leases.Get(ctx, types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name + "-runner"}, &lease)
	return lease, client.IgnoreNotFound(err)
}

// unquiescedRunner is the Pod UID of the last connected runner if it still has Sessions to seal, or
// "": none connected, or it is the one already sealed.
func (r *Reconciler) unquiescedRunner(ctx context.Context, sandbox *Sandbox) (string, error) {
	runner, err := r.connectedRunner(ctx, sandbox)
	if err != nil || sandbox.quiescedIncarnation(runner) {
		return "", err
	}
	return runner, nil
}

// quiesceBeforeStop holds a Sandbox that is to stop while its current incarnation's runner has Sessions
// to seal, and seals them. An incarnation whose runner never connected has none.
func (r *Reconciler) quiesceBeforeStop(ctx context.Context, sandbox *Sandbox, w workload) (*unready, error) {
	pod := w.incarnation()
	if pod == nil {
		return nil, nil
	}
	runner, err := r.unquiescedRunner(ctx, sandbox)
	if err != nil || runner == "" || runner != string(pod.UID) {
		return nil, err
	}
	if !sandbox.quiescing() {
		if held, err := r.holdSuspend(ctx, sandbox, runner); held != nil || err != nil {
			return held, err
		}
	}
	return r.quiesce(ctx, sandbox, runner)
}

// holdSuspend holds a suspend that has not begun quiescing while the incarnation's bootstrap or any
// Session's setup runs: quiescing would interrupt it, and an interrupted bootstrap is terminal. The
// runner's next fact enqueues the Sandbox, so no retry is set. A delete does not come here.
func (r *Reconciler) holdSuspend(ctx context.Context, sandbox *Sandbox, runner string) (*unready, error) {
	lease, err := r.runnerLease(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	facts, err := readRunnerFacts(&lease, runner)
	if err != nil {
		return nil, err
	}
	switch {
	case facts.Bootstrap == BootstrapRunning:
		return &unready{reason: "SuspendPending", message: "the Sandbox's bootstrap is running; the suspend waits for it"}, nil
	case facts.SetupsRunning:
		return &unready{reason: "SuspendPending", message: "a Session's setup is running; the suspend waits for it"}, nil
	}
	return nil, nil
}

// quiesce asks the runner of the Pod with this UID to seal every Session and, once the ingress has
// acknowledged each seal, records it on the Sandbox's status for the caller to write. The returned
// unready always says why the Sandbox is still held and has no retryAfter once the seal is recorded.
// Before each call, the controller persists its attempt time so watch events and restarts cannot
// bypass the one-minute retry deadline.
//
// A runner that reports `Quiesced` on the connection Lease is recorded without another call. Other
// stage reports enqueue the Sandbox; the persisted deadline throttles calls while a stage is absent
// or stalled.
//
// CLEANUP(added 2026-10-14): Remove the call's reading of a runner that reports no stage once the
// runner's non-blocking `Quiesce` is pinned in every environment, the VM guest's wheel included.
func (r *Reconciler) quiesce(ctx context.Context, sandbox *Sandbox, runner string) (*unready, error) {
	lease, err := r.runnerLease(ctx, sandbox)
	if err != nil {
		return &unready{reason: "Quiescing", message: err.Error(), retryAfter: quiesceDeadline}, nil
	}
	facts, err := readRunnerFacts(&lease, runner)
	if err != nil {
		return &unready{reason: "Quiescing", message: err.Error(), retryAfter: quiesceDeadline}, nil
	}
	switch facts.Stage {
	case QuiesceQuiesced:
		sandbox.Status.QuiescedIncarnation = &runner
		sandbox.Status.QuiesceAttempt = nil
		return &unready{reason: "Quiescing", message: "the runner has quiesced every Session"}, nil
	}
	if attempt := sandbox.Status.QuiesceAttempt; attempt != nil && attempt.RunnerPodUID == runner {
		if remaining := attempt.AttemptedAt.Add(quiesceDeadline).Sub(r.Now()); remaining > 0 {
			return &unready{reason: "Quiescing", message: "the runner is quiescing", retryAfter: remaining}, nil
		}
	}
	sandbox.Status.QuiesceAttempt = &QuiesceAttempt{RunnerPodUID: runner, AttemptedAt: metav1.NewTime(r.Now())}
	if err := r.Status().Update(ctx, sandbox); err != nil {
		return nil, fmt.Errorf("recording quiesce attempt for %s: %w", sandbox.Name, err)
	}
	result, err := r.Quiescer.Quiesce(ctx, sandbox)
	if err != nil {
		return &unready{reason: "Quiescing", message: err.Error(), retryAfter: quiesceDeadline}, nil
	}
	if result.Quiescing {
		return &unready{reason: "Quiescing", message: "the runner has not finished quiescing", retryAfter: quiesceDeadline}, nil
	}
	if len(result.Unflushed) > 0 {
		return &unready{reason: "Quiescing", message: fmt.Sprintf("the ingress has not acknowledged the seal of %v", result.Unflushed), retryAfter: quiesceDeadline}, nil
	}
	sandbox.Status.QuiescedIncarnation = &runner
	sandbox.Status.QuiesceAttempt = nil
	return &unready{reason: "Quiescing", message: fmt.Sprintf("%d Sessions sealed", result.Sessions)}, nil
}

// deleteIfLive deletes obj, that very object, unless its deletion has begun.
func (r *Reconciler) deleteIfLive(ctx context.Context, obj client.Object) error {
	if !obj.GetDeletionTimestamp().IsZero() {
		return nil
	}
	uid := obj.GetUID()
	return client.IgnoreNotFound(r.Delete(ctx, obj, client.Preconditions{UID: &uid}))
}

func (r *Reconciler) stateVolume(ctx context.Context, sandbox *Sandbox) (*corev1.PersistentVolumeClaim, error) {
	return r.claim(ctx, sandbox, StateVolumeName(sandbox.Name))
}

// claim is the PVC of that name in the Sandbox's namespace, or nil.
func (r *Reconciler) claim(ctx context.Context, sandbox *Sandbox, name string) (*corev1.PersistentVolumeClaim, error) {
	var pvc corev1.PersistentVolumeClaim
	if err := r.Get(ctx, types.NamespacedName{Namespace: sandbox.Namespace, Name: name}, &pvc); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	return &pvc, nil
}

// claimConflict is why the Sandbox cannot use a PVC of its name: it is another's, or going.
func claimConflict(pvc *corev1.PersistentVolumeClaim, sandbox *Sandbox, reason string) *unready {
	if metav1.IsControlledBy(pvc, sandbox) && pvc.DeletionTimestamp.IsZero() {
		return nil
	}
	return &unready{reason: reason, message: fmt.Sprintf("PVC %s belongs to another Sandbox or is being deleted", pvc.Name)}
}

func (r *Reconciler) template(ctx context.Context, sandbox *Sandbox) (*SandboxTemplate, *unready, error) {
	var template SandboxTemplate
	key := types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Spec.Template}
	if err := r.Get(ctx, key, &template); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, &unready{reason: "TemplateNotFound", message: fmt.Sprintf("SandboxTemplate %s not found", sandbox.Spec.Template)}, nil
		}
		return nil, nil, err
	}
	if (template.Spec.Pod == nil) == (template.Spec.VM == nil) {
		return nil, &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s must set exactly one of pod and vm", template.Name)}, nil
	}
	if blocked := validateStateVolume(&template); blocked != nil {
		return nil, blocked, nil
	}
	if blocked := validateWorkspace(&template); blocked != nil {
		return nil, blocked, nil
	}
	return &template, nil, nil
}

// converge starts a Pod or VM for a Sandbox that is to run and has none, unless gate says why not
// yet, and stops the one of a Sandbox that is not. A running incarnation keeps its kind and spec
// whatever the template now says. It returns the workload the Sandbox now has.
func (r *Reconciler) converge(ctx context.Context, sandbox *Sandbox, w workload, running bool, gate *unready) (workload, *unready, error) {
	if !running {
		if w.pod != nil {
			if err := r.deleteIfLive(ctx, w.pod); err != nil {
				return w, nil, err
			}
		}
		return w, nil, r.haltVM(ctx, w.vm)
	}
	// Validate retained contents before replacing a sealed/ended incarnation or a halted VM.
	ended := w.pod != nil && (w.pod.Status.Phase == corev1.PodSucceeded || w.pod.Status.Phase == corev1.PodFailed)
	sealed := w.incarnation() != nil && sandbox.quiescedIncarnation(string(w.incarnation().UID))
	if gate != nil && (sealed || ended) {
		return w, gate, nil
	}
	var template *SandboxTemplate
	if gate == nil && (sealed || ended || (w.pod == nil && !vmRunning(w.vm) && w.vmi == nil)) {
		var blocked *unready
		var err error
		template, blocked, err = r.template(ctx, sandbox)
		if template == nil {
			return w, blocked, err
		}
		if blocked, err := r.ensureWorkspaceVolume(ctx, sandbox, template); blocked != nil || err != nil {
			return w, blocked, err
		}
	}
	// A runner that sealed its Sessions takes no new ones, so a Sandbox resumed meanwhile starts anew.
	if pod := w.incarnation(); pod != nil && sandbox.quiescedIncarnation(string(pod.UID)) {
		if err := r.deleteIfLive(ctx, pod); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "Starting", message: fmt.Sprintf("Pod %s is sealed and is being replaced", pod.Name)}, nil
	}
	if w.pod != nil {
		// An ended incarnation goes too, so a running Sandbox gets a new one. A Pod still terminating
		// keeps its name; the next one starts after its deletion event.
		if w.pod.Status.Phase == corev1.PodSucceeded || w.pod.Status.Phase == corev1.PodFailed {
			if err := r.deleteIfLive(ctx, w.pod); err != nil {
				return w, nil, err
			}
			return w, nil, nil
		}
		return w, nil, nil
	}
	// KubeVirt replaces a running VM's VMI that ends. A stopped VM's last VMI still stopping, or a VM
	// being deleted, holds the next start until its deletion event.
	if (w.vm != nil && (vmRunning(w.vm) || !w.vm.DeletionTimestamp.IsZero())) || w.vmi != nil {
		return w, nil, nil
	}
	if gate != nil {
		return w, gate, nil
	}
	// Render the same snapshot whose layout and binding passed the precheck.
	if template.Spec.VM != nil {
		if !r.KubeVirt {
			return w, &unready{reason: "KubeVirtMissing", message: "the cluster does not serve KubeVirt's API, so a VM Sandbox cannot run"}, nil
		}
		return r.startVM(ctx, sandbox, w, template)
	}
	if w.vm != nil {
		if len(w.vm.Spec.DataVolumeTemplates) != 0 || slices.ContainsFunc(w.vm.Spec.Template.Spec.Volumes, func(v kubevirtv1.Volume) bool { return v.DataVolume != nil }) {
			return w, layoutMismatch("a Pod transition cannot discard the retained VM's DataVolume disks"), nil
		}
		if err := r.deleteIfLive(ctx, w.vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "Starting", message: fmt.Sprintf("VM %s is being deleted for the Pod template", w.vm.Name)}, nil
	}
	pod, blocked := render(sandbox, template)
	if blocked != nil {
		return w, blocked, nil
	}
	if err := controllerutil.SetControllerReference(sandbox, pod, r.Scheme()); err != nil {
		return w, nil, err
	}
	if err := r.Create(ctx, pod); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// A Pod this Sandbox does not control holds the name.
			return w, &unready{reason: "PodConflict", message: fmt.Sprintf("Pod %s exists and is not this Sandbox's", pod.Name)}, nil
		}
		return w, nil, err
	}
	w.pod = pod
	return w, nil, nil
}

// renderedSpec is the annotation value for a rendered Pod or VM spec.
func renderedSpec(spec any) string {
	encoded, err := json.Marshal(spec)
	if err != nil {
		panic(err) // A rendered spec always marshals.
	}
	return string(encoded)
}

// render is the template's Pod for this Sandbox, running as the Sandbox's ServiceAccount, with the
// state volume filled in.
func render(sandbox *Sandbox, template *SandboxTemplate) (*corev1.Pod, *unready) {
	if blocked := validateStateVolume(template); blocked != nil {
		return nil, blocked
	}
	if blocked := validateWorkspace(template); blocked != nil {
		return nil, blocked
	}
	spec := template.Spec.Pod.Spec.DeepCopy()
	volume := template.Spec.StateVolume.Name
	if slices.ContainsFunc(spec.Volumes, func(v corev1.Volume) bool { return v.Name == volume }) {
		return nil, &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the state volume %s itself", template.Name, volume)}
	}
	spec.ServiceAccountName = sandbox.Name
	if blocked := withLaunch(sandbox, template, spec); blocked != nil {
		return nil, blocked
	}
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: volume,
		VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
			ClaimName: StateVolumeName(sandbox.Name),
		}},
	})
	if workspace := template.Spec.WorkspaceVolume; workspace != nil {
		spec.Volumes = append(spec.Volumes, corev1.Volume{
			Name: workspace.Name,
			VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
				ClaimName: WorkspaceVolumeName(sandbox.Name),
			}},
		})
	}
	annotations := maps.Clone(template.Spec.Pod.Annotations)
	if annotations == nil {
		annotations = map[string]string{}
	}
	annotations[RenderedSpecAnnotation] = renderedSpec(spec)
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        sandbox.Name,
			Namespace:   sandbox.Namespace,
			Labels:      withSandboxLabel(template.Spec.Pod.Labels, sandbox),
			Annotations: annotations,
		},
		Spec: *spec,
	}, nil
}

func withSandboxLabel(labels map[string]string, sandbox *Sandbox) map[string]string {
	labels = maps.Clone(labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[SandboxLabel] = sandbox.Name
	return labels
}

func (r *Reconciler) updateStatus(ctx context.Context, sandbox *Sandbox, w workload, blocked *unready) (ctrl.Result, error) {
	status := sandbox.Status.DeepCopy()
	status.Incarnation = nil
	status.OperatingMode = sandbox.operatingMode()
	if pod := w.incarnation(); pod != nil {
		status.Incarnation = &Incarnation{Name: pod.Name, UID: string(pod.UID)}
	}
	condition := metav1.Condition{Type: Running, Status: metav1.ConditionFalse, ObservedGeneration: sandbox.Generation}
	switch {
	case blocked != nil:
		condition.Reason, condition.Message = blocked.reason, blocked.message
	case w.pod != nil:
		podCondition(&condition, sandbox, w.pod)
	case w.vmi != nil || (w.vm != nil && vmRunning(w.vm)):
		vmCondition(&condition, sandbox, w)
	case sandbox.running():
		condition.Reason, condition.Message = "Starting", "no Pod or VM is created yet"
	case w.vm != nil:
		condition.Reason, condition.Message = "Stopped", fmt.Sprintf("VM %s is halted; its disks and the state PVC are kept", w.vm.Name)
	default:
		condition.Reason, condition.Message = "Stopped", "no Pod; the state PVC is kept"
	}
	meta.SetStatusCondition(&status.Conditions, condition)
	workloadReady := workloadReadyCondition(sandbox, condition, w.incarnation())
	meta.SetStatusCondition(&status.Conditions, workloadReady)
	ready := workloadReady
	connected, recheck, err := r.runnerConnectedCondition(ctx, sandbox, workloadReady, w.incarnation())
	if err != nil {
		return ctrl.Result{}, err
	}
	meta.SetStatusCondition(&status.Conditions, connected)
	bootstrapped, err := r.bootstrappedCondition(ctx, sandbox, w.incarnation())
	if err != nil {
		return ctrl.Result{}, err
	}
	meta.SetStatusCondition(&status.Conditions, bootstrapped)
	if workloadReady.Status == metav1.ConditionTrue {
		ready = connected
		if connected.Status == metav1.ConditionTrue && bootstrapped.Status != metav1.ConditionTrue {
			ready = bootstrapped
		}
	}
	ready.Type = Ready
	meta.SetStatusCondition(&status.Conditions, ready)
	result := ctrl.Result{RequeueAfter: earliest(blocked.retryAfterOrZero(), recheck)}
	if equality.Semantic.DeepEqual(status, &sandbox.Status) {
		return result, nil
	}
	sandbox.Status = *status
	return result, r.Status().Update(ctx, sandbox)
}

// earliest is the shorter of two requeue delays, where zero is none.
func earliest(a, b time.Duration) time.Duration {
	if a == 0 || (b != 0 && b < a) {
		return b
	}
	return a
}

// workloadReadyCondition is True only while the Sandbox runs and its incarnation Pod is Ready;
// otherwise it carries the reason `running` gives, or `PodNotReady` when everything runs but the Pod
// is not Ready.
func workloadReadyCondition(sandbox *Sandbox, running metav1.Condition, incarnation *corev1.Pod) metav1.Condition {
	condition := metav1.Condition{Type: WorkloadReady, Status: metav1.ConditionFalse, ObservedGeneration: sandbox.Generation, Reason: running.Reason, Message: running.Message}
	if running.Status != metav1.ConditionTrue {
		return condition
	}
	if podReady(incarnation) {
		condition.Status, condition.Reason = metav1.ConditionTrue, "PodReady"
		condition.Message = fmt.Sprintf("Pod %s is Ready", incarnation.Name)
	} else {
		condition.Reason, condition.Message = "PodNotReady", fmt.Sprintf("Pod %s is running but not Ready", incarnation.Name)
	}
	return condition
}

// runnerConnectedCondition is True while the Runner Gateway's Lease names this incarnation's Pod and
// was renewed within its duration plus the grace, so a connection from an earlier incarnation never
// counts and a brief drop does not. It also says when to look again, since no watch event marks time:
// the moment a live connection would lapse, and a lapsed one, which a renewal revives silently.
func (r *Reconciler) runnerConnectedCondition(ctx context.Context, sandbox *Sandbox, workload metav1.Condition, incarnation *corev1.Pod) (metav1.Condition, time.Duration, error) {
	condition := metav1.Condition{Type: RunnerConnected, Status: metav1.ConditionFalse, ObservedGeneration: sandbox.Generation}
	if workload.Status != metav1.ConditionTrue {
		condition.Reason, condition.Message = "WorkloadNotReady", "the incarnation is not Ready"
		return condition, 0, nil
	}
	var lease coordinationv1.Lease
	if err := r.Leases.Get(ctx, types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name + "-runner"}, &lease); err != nil {
		if !apierrors.IsNotFound(err) {
			return condition, 0, err
		}
		condition.Reason, condition.Message = "NeverConnected", "no runner has connected"
		return condition, 0, nil
	}
	if runner := lease.Annotations[PodUIDAnnotation]; runner == "" || runner != string(incarnation.UID) {
		condition.Reason, condition.Message = "AwaitingRunner", fmt.Sprintf("the runner that connected last is not Pod %s's", incarnation.Name)
		return condition, 0, nil
	}
	if lease.Spec.RenewTime == nil || lease.Spec.LeaseDurationSeconds == nil {
		condition.Reason, condition.Message = "ConnectionLost", "the connection Lease records no renewal"
		return condition, runnerLapsedPoll, nil
	}
	lapse := lease.Spec.RenewTime.Add(time.Duration(*lease.Spec.LeaseDurationSeconds)*time.Second + runnerDisconnectGrace)
	if remaining := lapse.Sub(r.Now()); remaining > 0 {
		condition.Status, condition.Reason = metav1.ConditionTrue, "Connected"
		condition.Message = fmt.Sprintf("Pod %s's runner is connected to the Runner Gateway", incarnation.Name)
		return condition, remaining, nil
	}
	condition.Reason, condition.Message = "ConnectionLost", fmt.Sprintf("Pod %s's runner has not renewed its connection since %s", incarnation.Name, lease.Spec.RenewTime.Format(time.RFC3339))
	return condition, runnerLapsedPoll, nil
}

// bootstrappedCondition says how far the Sandbox's bootstrap has got, from the facts the incarnation's
// runner published on its Lease. A Sandbox without one is bootstrapped. A runner that has reported
// nothing leaves the condition Unknown, since one that predates the fact never will.
func (r *Reconciler) bootstrappedCondition(ctx context.Context, sandbox *Sandbox, incarnation *corev1.Pod) (metav1.Condition, error) {
	condition := metav1.Condition{Type: Bootstrapped, Status: metav1.ConditionFalse, ObservedGeneration: sandbox.Generation}
	if sandbox.Spec.Bootstrap == "" {
		condition.Status, condition.Reason, condition.Message = metav1.ConditionTrue, "NoBootstrap", "the Sandbox has no bootstrap"
		return condition, nil
	}
	var facts RunnerFacts
	if incarnation != nil {
		lease, err := r.runnerLease(ctx, sandbox)
		if err != nil {
			return condition, err
		}
		if facts, err = readRunnerFacts(&lease, string(incarnation.UID)); err != nil {
			return condition, err
		}
	}
	switch facts.Bootstrap {
	case BootstrapNone:
		condition.Status = metav1.ConditionUnknown
		condition.Reason, condition.Message = "BootstrapNotReported", "the runner has not reported its bootstrap"
	case BootstrapRunning:
		condition.Reason, condition.Message = "BootstrapRunning", "the bootstrap is running"
	case BootstrapSucceeded:
		condition.Status, condition.Reason, condition.Message = metav1.ConditionTrue, "BootstrapSucceeded", "the bootstrap ran to success"
	case BootstrapFailed:
		condition.Reason, condition.Message = "BootstrapFailed", "the bootstrap failed and never runs again; the Sandbox is to be replaced"
	case BootstrapInterrupted:
		condition.Reason, condition.Message = "BootstrapFailed", "the bootstrap was interrupted and never runs again; the Sandbox is to be replaced"
	case BootstrapStateUnrecognised:
		condition.Reason, condition.Message = "BootstrapStateUnrecognised", "the runner reports a bootstrap state this controller does not know"
	}
	return condition, nil
}

func podReady(pod *corev1.Pod) bool {
	return pod != nil && slices.ContainsFunc(pod.Status.Conditions, func(c corev1.PodCondition) bool {
		return c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue
	})
}

func podCondition(condition *metav1.Condition, sandbox *Sandbox, pod *corev1.Pod) {
	switch {
	case !pod.DeletionTimestamp.IsZero() || !sandbox.running():
		condition.Reason, condition.Message = "Stopping", fmt.Sprintf("Pod %s is being deleted", pod.Name)
	case pod.Status.Phase == corev1.PodPending || pod.Status.Phase == "":
		if reason, message, scheduled := podSchedulingFailure(pod); scheduled {
			condition.Reason, condition.Message = reason, message
		} else {
			condition.Reason, condition.Message = "Starting", fmt.Sprintf("Pod %s is not running yet", pod.Name)
		}
	case pod.Status.Phase == corev1.PodRunning:
		condition.Status, condition.Reason, condition.Message = metav1.ConditionTrue, "PodRunning", fmt.Sprintf("Pod %s is running", pod.Name)
	default:
		condition.Reason, condition.Message = "PodEnded", fmt.Sprintf("Pod %s ended (%s); a new one replaces it", pod.Name, pod.Status.Phase)
	}
}

func podSchedulingFailure(pod *corev1.Pod) (string, string, bool) {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodScheduled && condition.Status == corev1.ConditionFalse && condition.Reason != "" {
			return condition.Reason, condition.Message, true
		}
	}
	return "", "", false
}

func podHasSchedulingFailure(pod *corev1.Pod) bool {
	_, _, failed := podSchedulingFailure(pod)
	return failed
}

// SetupWithManager wires the watches: each Sandbox, the objects it owns, the Pods, VMIs and state
// PVC carrying its label, and the templates it names.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &Sandbox{}, templateIndex, func(obj client.Object) []string {
		return []string{obj.(*Sandbox).Spec.Template}
	}); err != nil {
		return err
	}
	// A template appearing or changing retries the Sandboxes that name it; one already running is
	// unaffected until its next start.
	byTemplate := handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		var sandboxes SandboxList
		if err := r.List(ctx, &sandboxes, client.InNamespace(obj.GetNamespace()), client.MatchingFields{templateIndex: obj.GetName()}); err != nil {
			return nil
		}
		var out []reconcile.Request
		for _, s := range sandboxes.Items {
			out = append(out, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&s)})
		}
		return out
	})
	bySandboxLabel := handler.EnqueueRequestsFromMapFunc(func(_ context.Context, obj client.Object) []reconcile.Request {
		name, ok := obj.GetLabels()[SandboxLabel]
		if !ok {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name}}}
	})
	setup := ctrl.NewControllerManagedBy(mgr).
		For(&Sandbox{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&policycontroller.PolicyBinding{}).
		// A virt-launcher Pod is its VMI's, not the Sandbox's, so Pods map back by the label.
		Watches(&corev1.Pod{}, bySandboxLabel).
		Watches(&corev1.PersistentVolumeClaim{}, bySandboxLabel).
		Watches(&SandboxTemplate{}, byTemplate)
	// A connection's Lease is the Sandbox's by owner reference. Renewals only move the renew time,
	// so only a different runner Pod, a new connection or a change in the facts the runner
	// publishes enqueues.
	setup = setup.Watches(&coordinationv1.Lease{},
		handler.EnqueueRequestForOwner(mgr.GetScheme(), mgr.GetRESTMapper(), &Sandbox{}),
		builder.WithPredicates(predicate.Funcs{UpdateFunc: func(e event.UpdateEvent) bool {
			previous, current := e.ObjectOld.(*coordinationv1.Lease), e.ObjectNew.(*coordinationv1.Lease)
			return previous.Annotations[PodUIDAnnotation] != current.Annotations[PodUIDAnnotation] ||
				leaseTransitions(previous) != leaseTransitions(current) ||
				runnerFactsChanged(previous, current)
		}}))
	if r.KubeVirt {
		setup = setup.Owns(&kubevirtv1.VirtualMachine{}).Watches(&kubevirtv1.VirtualMachineInstance{}, bySandboxLabel)
	}
	return setup.Complete(r)
}

// leaseTransitions is how many connections the Runner Gateway has accepted for the Sandbox.
func leaseTransitions(lease *coordinationv1.Lease) int32 {
	if lease.Spec.LeaseTransitions == nil {
		return 0
	}
	return *lease.Spec.LeaseTransitions
}
