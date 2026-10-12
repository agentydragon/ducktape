package sandboxcontroller

import (
	"context"
	"fmt"
	"maps"
	"slices"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

const (
	Running = "Running"
	// Set True by the Runner Gateway on a deleting Sandbox once its runner has sealed every Session
	// and the ingress has acknowledged each seal; nothing of the Sandbox is deleted before.
	SessionsSealed = "SessionsSealed"
	// On the Pod, the state PVC, the ServiceAccount and the PolicyBinding: the Sandbox they belong to.
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// On the state PVC, which has no owner reference: the Sandbox incarnation that created it, so a
	// recreated Sandbox of the same name never takes over a previous one's state.
	SandboxUIDAnnotation = "agentplane.allegedly.works/sandbox-uid"
	// Seals a deleted Sandbox's Sessions, then deletes its Pod and, once that is gone, its state PVC.
	StateVolumeFinalizer = "agentplane.allegedly.works/state-volume"

	templateIndex = "spec.template"
)

// Reconciler converges one Sandbox at a time: its state PVC, its Pod, and its Running condition.
//
// +kubebuilder:object:generate=false
type Reconciler struct {
	client.Client
}

// StateVolumeName is the Sandbox's state PVC.
func StateVolumeName(sandbox string) string { return sandbox + "-state" }

// unready is why the Sandbox cannot have a Pod; its reason and message go on the Running condition.
//
// +kubebuilder:object:generate=false
type unready struct{ reason, message string }

func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (ctrl.Result, error) {
	var sandbox Sandbox
	if err := r.Get(ctx, req.NamespacedName, &sandbox); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	pod, err := r.pod(ctx, &sandbox)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !sandbox.DeletionTimestamp.IsZero() {
		if !controllerutil.ContainsFinalizer(&sandbox, StateVolumeFinalizer) {
			return ctrl.Result{}, nil
		}
		pod, blocked, err := r.finalize(ctx, &sandbox, pod)
		if err != nil || blocked == nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, r.updateStatus(ctx, &sandbox, pod, blocked)
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
	if blocked == nil {
		if pod, blocked, err = r.convergePod(ctx, &sandbox, pod, sandbox.Spec.Running, gate); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, r.updateStatus(ctx, &sandbox, pod, blocked)
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
func (r *Reconciler) finalize(ctx context.Context, sandbox *Sandbox, pod *corev1.Pod) (*corev1.Pod, *unready, error) {
	pvc, err := r.stateVolume(ctx, sandbox)
	if err != nil {
		return nil, nil, err
	}
	own := pvc != nil && pvc.Annotations[SandboxUIDAnnotation] == string(sandbox.UID)
	// Without a state PVC of its own the Sandbox never had a runner, so it has no Session to seal.
	if own && !meta.IsStatusConditionTrue(sandbox.Status.Conditions, SessionsSealed) {
		// Only a running runner seals, so a stopped Sandbox runs once more; the Runner Gateway
		// refuses every new Session and command for a deleting Sandbox.
		pod, blocked, err := r.convergePod(ctx, sandbox, pod, true, nil)
		if blocked == nil {
			blocked = &unready{"Sealing", "waiting for the Runner Gateway to seal every Session"}
		}
		return pod, blocked, err
	}
	if pod != nil {
		if pod.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, pod, client.Preconditions{UID: &pod.UID})); err != nil {
				return nil, nil, err
			}
		}
		return pod, &unready{"Deleting", fmt.Sprintf("Pod %s is being deleted", pod.Name)}, nil
	}
	if own {
		if pvc.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, pvc, client.Preconditions{UID: &pvc.UID})); err != nil {
				return nil, nil, err
			}
		}
		return nil, &unready{"Deleting", fmt.Sprintf("PVC %s is being deleted", pvc.Name)}, nil
	}
	controllerutil.RemoveFinalizer(sandbox, StateVolumeFinalizer)
	return nil, nil, r.Update(ctx, sandbox)
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
			return &unready{"StateVolumeConflict", fmt.Sprintf("PVC %s belongs to another Sandbox or is being deleted", pvc.Name)}, nil
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
			return nil, &unready{"TemplateNotFound", fmt.Sprintf("SandboxTemplate %s not found", sandbox.Spec.Template)}, nil
		}
		return nil, nil, err
	}
	return &template, nil, nil
}

// convergePod starts a Pod for a Sandbox that is to run and has none, unless gate says why not
// yet, and deletes the Pod of one that is not. It returns the Pod the Sandbox now has.
func (r *Reconciler) convergePod(ctx context.Context, sandbox *Sandbox, pod *corev1.Pod, running bool, gate *unready) (*corev1.Pod, *unready, error) {
	if pod != nil {
		ended := pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed
		if pod.DeletionTimestamp.IsZero() && (!running || ended) {
			// An ended incarnation goes too, so a running Sandbox gets a new one.
			if err := client.IgnoreNotFound(r.Delete(ctx, pod, client.Preconditions{UID: &pod.UID})); err != nil {
				return nil, nil, err
			}
		}
		// A Pod still terminating keeps its name; the next one starts after its deletion event.
		return pod, nil, nil
	}
	if !running {
		return nil, nil, nil
	}
	if gate != nil {
		return nil, gate, nil
	}
	template, blocked, err := r.template(ctx, sandbox)
	if template == nil {
		return nil, blocked, err
	}
	pod, blocked = render(sandbox, template)
	if blocked != nil {
		return nil, blocked, nil
	}
	if err := controllerutil.SetControllerReference(sandbox, pod, r.Scheme()); err != nil {
		return nil, nil, err
	}
	if err := r.Create(ctx, pod); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// A Pod this Sandbox does not control holds the name.
			return nil, &unready{"PodConflict", fmt.Sprintf("Pod %s exists and is not this Sandbox's", pod.Name)}, nil
		}
		return nil, nil, err
	}
	return pod, nil, nil
}

// render is the template's Pod for this Sandbox, running as the Sandbox's ServiceAccount, with the
// state volume filled in.
func render(sandbox *Sandbox, template *SandboxTemplate) (*corev1.Pod, *unready) {
	spec := template.Spec.Pod.Spec.DeepCopy()
	volume := template.Spec.StateVolume.Name
	if slices.ContainsFunc(spec.Volumes, func(v corev1.Volume) bool { return v.Name == volume }) {
		return nil, &unready{"TemplateInvalid", fmt.Sprintf("SandboxTemplate %s defines the state volume %s itself", template.Name, volume)}
	}
	spec.ServiceAccountName = sandbox.Name
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: volume,
		VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
			ClaimName: StateVolumeName(sandbox.Name),
		}},
	})
	labels := maps.Clone(template.Spec.Pod.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[SandboxLabel] = sandbox.Name
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        sandbox.Name,
			Namespace:   sandbox.Namespace,
			Labels:      labels,
			Annotations: maps.Clone(template.Spec.Pod.Annotations),
		},
		Spec: *spec,
	}, nil
}

func (r *Reconciler) updateStatus(ctx context.Context, sandbox *Sandbox, pod *corev1.Pod, blocked *unready) error {
	status := sandbox.Status.DeepCopy()
	status.Incarnation = nil
	if pod != nil {
		status.Incarnation = &Incarnation{Name: pod.Name, UID: string(pod.UID)}
	}
	condition := metav1.Condition{Type: Running, Status: metav1.ConditionFalse, ObservedGeneration: sandbox.Generation}
	switch {
	case blocked != nil:
		condition.Reason, condition.Message = blocked.reason, blocked.message
	case pod == nil && !sandbox.Spec.Running:
		condition.Reason, condition.Message = "Stopped", "no Pod; the state PVC is kept"
	case pod == nil:
		condition.Reason, condition.Message = "Starting", "the Pod is not created yet"
	case !pod.DeletionTimestamp.IsZero() || !sandbox.Spec.Running:
		condition.Reason, condition.Message = "Stopping", fmt.Sprintf("Pod %s is being deleted", pod.Name)
	case pod.Status.Phase == corev1.PodPending || pod.Status.Phase == "":
		condition.Reason, condition.Message = "Starting", fmt.Sprintf("Pod %s is not running yet", pod.Name)
	case pod.Status.Phase == corev1.PodRunning:
		condition.Status, condition.Reason, condition.Message = metav1.ConditionTrue, "PodRunning", fmt.Sprintf("Pod %s is running", pod.Name)
	default:
		condition.Reason, condition.Message = "PodEnded", fmt.Sprintf("Pod %s ended (%s); a new one replaces it", pod.Name, pod.Status.Phase)
	}
	meta.SetStatusCondition(&status.Conditions, condition)
	if equality.Semantic.DeepEqual(status, &sandbox.Status) {
		return nil
	}
	sandbox.Status = *status
	return r.Status().Update(ctx, sandbox)
}

// SetupWithManager wires the watches: each Sandbox, the objects it owns, its state PVC, and the
// templates it names.
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
	return ctrl.NewControllerManagedBy(mgr).
		For(&Sandbox{}).
		Owns(&corev1.Pod{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&policycontroller.PolicyBinding{}).
		Watches(&corev1.PersistentVolumeClaim{}, bySandboxLabel).
		Watches(&SandboxTemplate{}, byTemplate).
		Complete(r)
}
