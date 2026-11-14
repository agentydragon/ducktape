package main

import (
	"context"
	"math"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	// The operational marker of a node expected to leave and rejoin (set by the laptops' NixOS
	// `nodeTaints`); the same key the roaming alert exclusions match.
	roamingTaintKey = "node-role.kubernetes.io/roaming"
	// Where each kubelet renews a Lease named after its Node.
	nodeLeaseNamespace = "kube-node-lease"
	// How long a roaming kubelet must be silent before its node is marked out of service. Longer
	// than a suspend/resume or a Nebula re-handshake, so a brief blip never evicts anything; the
	// asymmetry with the immediate untaint bounds how often a flapping link churns pods.
	staleAfter = 5 * time.Minute
	// The kubelet's default when a Lease omits `leaseDurationSeconds`.
	defaultLeaseDuration = 40 * time.Second
	// Silence of a kubelet with no Lease or renew time on record.
	neverRenewed = time.Duration(math.MaxInt64)
	// Lease stale but the node still Ready: the node lifecycle controller has yet to notice.
	readyRecheck = 30 * time.Second
)

// Kubernetes' non-graceful node shutdown taint. Only this key makes the pod garbage collector
// force-delete the node's Terminating pods; a custom taint would leave them holding DaemonSet
// rollout budget forever. Value as documented upstream.
var outOfServiceTaint = corev1.Taint{
	Key:    corev1.TaintNodeOutOfService,
	Value:  "nodeshutdown",
	Effect: corev1.TaintEffectNoExecute,
}

// Reconciler marks roaming nodes whose kubelet has gone silent as out of service, and unmarks
// them as soon as the kubelet renews its Lease again.
type Reconciler struct {
	Client client.Client
	Now    func() time.Time
}

func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (reconcile.Result, error) {
	var node corev1.Node
	if err := r.Client.Get(ctx, req.NamespacedName, &node); err != nil {
		return reconcile.Result{}, client.IgnoreNotFound(err)
	}
	if !hasTaint(node.Spec.Taints, roamingTaintKey) {
		return reconcile.Result{}, nil
	}
	silence, leaseDuration, err := r.kubeletSilence(ctx, node.Name)
	if err != nil {
		return reconcile.Result{}, err
	}
	logger := log.FromContext(ctx).WithValues("node", node.Name, "silence", silence.String())
	if hasTaint(node.Spec.Taints, outOfServiceTaint.Key) {
		if silence >= leaseDuration {
			// Still away; the next Lease renewal wakes us.
			return reconcile.Result{}, nil
		}
		logger.Info("kubelet is back; removing out-of-service taint")
		return reconcile.Result{}, r.setTaints(ctx, &node, withoutTaint(node.Spec.Taints, outOfServiceTaint.Key))
	}
	if silence < staleAfter {
		return reconcile.Result{RequeueAfter: staleAfter - silence}, nil
	}
	if isReady(&node) {
		return reconcile.Result{RequeueAfter: readyRecheck}, nil
	}
	logger.Info("kubelet silent; adding out-of-service taint")
	taint := outOfServiceTaint
	added := metav1.NewTime(r.Now())
	taint.TimeAdded = &added
	return reconcile.Result{}, r.setTaints(ctx, &node, append(append([]corev1.Taint{}, node.Spec.Taints...), taint))
}

// kubeletSilence is how long ago the node's kubelet last renewed its Lease. A missing Lease or
// renew time means the kubelet has never reported, so it counts as silent indefinitely.
func (r *Reconciler) kubeletSilence(ctx context.Context, nodeName string) (time.Duration, time.Duration, error) {
	var lease coordinationv1.Lease
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: nodeLeaseNamespace, Name: nodeName}, &lease)
	if apierrors.IsNotFound(err) {
		return neverRenewed, defaultLeaseDuration, nil
	}
	if err != nil {
		return 0, 0, err
	}
	leaseDuration := defaultLeaseDuration
	if lease.Spec.LeaseDurationSeconds != nil {
		leaseDuration = time.Duration(*lease.Spec.LeaseDurationSeconds) * time.Second
	}
	if lease.Spec.RenewTime == nil {
		return neverRenewed, leaseDuration, nil
	}
	return r.Now().Sub(lease.Spec.RenewTime.Time), leaseDuration, nil
}

// setTaints patches with an optimistic lock: the node lifecycle controller edits taints on
// these same nodes, and a merge patch replaces the whole list.
func (r *Reconciler) setTaints(ctx context.Context, node *corev1.Node, taints []corev1.Taint) error {
	patch := client.MergeFromWithOptions(node.DeepCopy(), client.MergeFromWithOptimisticLock{})
	node.Spec.Taints = taints
	return r.Client.Patch(ctx, node, patch)
}

func hasTaint(taints []corev1.Taint, key string) bool {
	for _, t := range taints {
		if t.Key == key {
			return true
		}
	}
	return false
}

func withoutTaint(taints []corev1.Taint, key string) []corev1.Taint {
	kept := make([]corev1.Taint, 0, len(taints))
	for _, t := range taints {
		if t.Key != key {
			kept = append(kept, t)
		}
	}
	return kept
}

func isReady(node *corev1.Node) bool {
	for _, c := range node.Status.Conditions {
		if c.Type == corev1.NodeReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

// SetupWithManager reconciles a Node on its own changes and on every renewal of its kubelet
// Lease, so an untaint follows the first renewal without polling.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Node{}).
		Watches(
			&coordinationv1.Lease{},
			handler.EnqueueRequestsFromMapFunc(func(_ context.Context, lease client.Object) []reconcile.Request {
				return []reconcile.Request{{NamespacedName: types.NamespacedName{Name: lease.GetName()}}}
			}),
			builder.WithPredicates(predicate.NewPredicateFuncs(func(lease client.Object) bool {
				return lease.GetNamespace() == nodeLeaseNamespace
			})),
		).
		Complete(r)
}
