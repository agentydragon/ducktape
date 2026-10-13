package sandboxcontroller

import (
	"context"
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
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

const (
	Running = "Running"
	// Set True on a deleting Sandbox once its runner has sealed every Session and the ingress has
	// acknowledged each seal, or once no runner ever connected; nothing of the Sandbox is deleted before.
	SessionsSealed = "SessionsSealed"
	// On the Pod or the VirtualMachine and its VMI and virt-launcher Pod, the state PVC, the
	// ServiceAccount and the PolicyBinding: the Sandbox they belong to.
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// On the state PVC, which has no owner reference: the Sandbox incarnation that created it, so a
	// recreated Sandbox of the same name never takes over a previous one's state.
	SandboxUIDAnnotation = "agentplane.allegedly.works/sandbox-uid"
	// Seals a deleted Sandbox's Sessions, then deletes its Pod and, once that is gone, its state PVC.
	StateVolumeFinalizer = "agentplane.allegedly.works/state-volume"

	templateIndex = "spec.template"
	// How soon a seal that failed or is not yet acknowledged is asked again.
	sealRetry = 2 * time.Second
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
	// Seals a deleting Sandbox's Sessions through its runner.
	Sealer Sealer
	// Reads the Lease the Runner Gateway writes for each connection, uncached.
	Leases client.Reader
}

// StateVolumeName is the Sandbox's state PVC.
func StateVolumeName(sandbox string) string { return sandbox + "-state" }

// unready is why the Sandbox cannot run; its reason and message go on the Running condition.
//
// +kubebuilder:object:generate=false
type unready struct {
	reason, message string
	// Nonzero where no watch event will end the wait.
	retryAfter time.Duration
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
		return ctrl.Result{RequeueAfter: blocked.retryAfter}, r.updateStatus(ctx, &sandbox, w, blocked)
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
	if launchGate, err := r.ensureLaunch(ctx, &sandbox); err != nil {
		return ctrl.Result{}, err
	} else if gate == nil {
		gate = launchGate
	}
	if blocked == nil {
		if w, blocked, err = r.converge(ctx, &sandbox, w, sandbox.Spec.Running, gate); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, r.updateStatus(ctx, &sandbox, w, blocked)
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
	own := pvc != nil && pvc.Annotations[SandboxUIDAnnotation] == string(sandbox.UID)
	// Without a state PVC of its own the Sandbox never had a runner, so it has no Session to seal.
	if own && !meta.IsStatusConditionTrue(sandbox.Status.Conditions, SessionsSealed) {
		// Only a running runner seals, so a stopped Sandbox runs once more; the Runner Gateway
		// refuses every new Session and command for a deleting Sandbox.
		w, blocked, err := r.converge(ctx, sandbox, w, true, nil)
		if err != nil || blocked != nil {
			return w, blocked, err
		}
		blocked, err = r.seal(ctx, sandbox)
		return w, blocked, err
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
	if own {
		if err := r.deleteIfLive(ctx, pvc); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "Deleting", message: fmt.Sprintf("PVC %s is being deleted", pvc.Name)}, nil
	}
	controllerutil.RemoveFinalizer(sandbox, StateVolumeFinalizer)
	return w, nil, r.Update(ctx, sandbox)
}

// seal asks the runner to seal every Session and, once the ingress has acknowledged each seal,
// records it on the Sandbox's status for the caller to write. A Sandbox whose runner never connected
// has none to seal. Seal is idempotent, so any failure is asked again.
func (r *Reconciler) seal(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	var lease coordinationv1.Lease
	err := r.Leases.Get(ctx, types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name + "-runner"}, &lease)
	sealed := metav1.Condition{Type: SessionsSealed, Status: metav1.ConditionTrue, Reason: "Sealed", ObservedGeneration: sandbox.Generation}
	switch {
	case apierrors.IsNotFound(err):
		sealed.Message = "no runner ever connected"
	case err != nil:
		return nil, err
	default:
		result, err := r.Sealer.Seal(ctx, sandbox)
		if err != nil {
			return &unready{reason: "Sealing", message: err.Error(), retryAfter: sealRetry}, nil
		}
		if len(result.Unflushed) > 0 {
			return &unready{reason: "Sealing", message: fmt.Sprintf("the ingress has not acknowledged the seal of %v", result.Unflushed), retryAfter: sealRetry}, nil
		}
		sealed.Message = fmt.Sprintf("%d Sessions sealed", result.Sessions)
	}
	meta.SetStatusCondition(&sandbox.Status.Conditions, sealed)
	return &unready{reason: "Sealing", message: sealed.Message}, nil
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
	var pvc corev1.PersistentVolumeClaim
	key := types.NamespacedName{Namespace: sandbox.Namespace, Name: StateVolumeName(sandbox.Name)}
	if err := r.Get(ctx, key, &pvc); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	return &pvc, nil
}

// ensureStateVolume creates the state PVC from the template if it does not exist yet. It has no
// owner reference: nothing but the Sandbox's own deletion may garbage-collect it.
func (r *Reconciler) ensureStateVolume(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	pvc, err := r.stateVolume(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	if pvc != nil {
		if pvc.Annotations[SandboxUIDAnnotation] != string(sandbox.UID) || !pvc.DeletionTimestamp.IsZero() {
			return &unready{reason: "StateVolumeConflict", message: fmt.Sprintf("PVC %s belongs to another Sandbox or is being deleted", pvc.Name)}, nil
		}
		return nil, nil
	}
	template, blocked, err := r.template(ctx, sandbox)
	if template == nil {
		return blocked, err
	}
	pvc = &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:        StateVolumeName(sandbox.Name),
			Namespace:   sandbox.Namespace,
			Labels:      map[string]string{SandboxLabel: sandbox.Name},
			Annotations: map[string]string{SandboxUIDAnnotation: string(sandbox.UID)},
		},
		Spec: *template.Spec.StateVolume.Claim.DeepCopy(),
	}
	if err := r.Create(ctx, pvc); err != nil && !apierrors.IsAlreadyExists(err) {
		return nil, err
	}
	// An AlreadyExists is a concurrent create; the PVC's watch event runs the check above.
	return nil, nil
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
	if w.pod != nil {
		// An ended incarnation goes too, so a running Sandbox gets a new one. A Pod still terminating
		// keeps its name; the next one starts after its deletion event.
		if w.pod.Status.Phase == corev1.PodSucceeded || w.pod.Status.Phase == corev1.PodFailed {
			if err := r.deleteIfLive(ctx, w.pod); err != nil {
				return w, nil, err
			}
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
	template, blocked, err := r.template(ctx, sandbox)
	if template == nil {
		return w, blocked, err
	}
	if template.Spec.VM != nil {
		if !r.KubeVirt {
			return w, &unready{reason: "KubeVirtMissing", message: "the cluster does not serve KubeVirt's API, so a VM Sandbox cannot run"}, nil
		}
		return r.startVM(ctx, sandbox, w, template)
	}
	if w.vm != nil {
		// A Pod cannot use the VM's disks, so switching to a Pod template drops the VM-local
		// workspace; the state PVC stays.
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

// render is the template's Pod for this Sandbox, running as the Sandbox's ServiceAccount, with the
// state volume filled in.
func render(sandbox *Sandbox, template *SandboxTemplate) (*corev1.Pod, *unready) {
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
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        sandbox.Name,
			Namespace:   sandbox.Namespace,
			Labels:      withSandboxLabel(template.Spec.Pod.Labels, sandbox),
			Annotations: maps.Clone(template.Spec.Pod.Annotations),
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

func (r *Reconciler) updateStatus(ctx context.Context, sandbox *Sandbox, w workload, blocked *unready) error {
	status := sandbox.Status.DeepCopy()
	status.Incarnation = nil
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
	case sandbox.Spec.Running:
		condition.Reason, condition.Message = "Starting", "no Pod or VM is created yet"
	case w.vm != nil:
		condition.Reason, condition.Message = "Stopped", fmt.Sprintf("VM %s is halted; its disks and the state PVC are kept", w.vm.Name)
	default:
		condition.Reason, condition.Message = "Stopped", "no Pod; the state PVC is kept"
	}
	meta.SetStatusCondition(&status.Conditions, condition)
	if equality.Semantic.DeepEqual(status, &sandbox.Status) {
		return nil
	}
	sandbox.Status = *status
	return r.Status().Update(ctx, sandbox)
}

func podCondition(condition *metav1.Condition, sandbox *Sandbox, pod *corev1.Pod) {
	switch {
	case !pod.DeletionTimestamp.IsZero() || !sandbox.Spec.Running:
		condition.Reason, condition.Message = "Stopping", fmt.Sprintf("Pod %s is being deleted", pod.Name)
	case pod.Status.Phase == corev1.PodPending || pod.Status.Phase == "":
		condition.Reason, condition.Message = "Starting", fmt.Sprintf("Pod %s is not running yet", pod.Name)
	case pod.Status.Phase == corev1.PodRunning:
		condition.Status, condition.Reason, condition.Message = metav1.ConditionTrue, "PodRunning", fmt.Sprintf("Pod %s is running", pod.Name)
	default:
		condition.Reason, condition.Message = "PodEnded", fmt.Sprintf("Pod %s ended (%s); a new one replaces it", pod.Name, pod.Status.Phase)
	}
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
	builder := ctrl.NewControllerManagedBy(mgr).
		For(&Sandbox{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&policycontroller.PolicyBinding{}).
		// A virt-launcher Pod is its VMI's, not the Sandbox's, so Pods map back by the label.
		Watches(&corev1.Pod{}, bySandboxLabel).
		Watches(&corev1.PersistentVolumeClaim{}, bySandboxLabel).
		Watches(&SandboxTemplate{}, byTemplate)
	if r.KubeVirt {
		builder = builder.Owns(&kubevirtv1.VirtualMachine{}).Watches(&kubevirtv1.VirtualMachineInstance{}, bySandboxLabel)
	}
	return builder.Complete(r)
}
