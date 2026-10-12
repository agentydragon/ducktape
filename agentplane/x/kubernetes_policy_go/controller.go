package kubernetespolicy

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"slices"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	KubernetesReady    = "KubernetesReady"
	FieldManager       = "agentplane-kubernetes-policy-controller"
	ManagedByLabel     = "app.kubernetes.io/managed-by"
	PolicyNamespaceLbl = "kubernetes-policy.agentplane.allegedly.works/policy-namespace"
	BindingAnnotation  = "kubernetes-policy.agentplane.allegedly.works/binding"
	BindingUIDAnn      = "kubernetes-policy.agentplane.allegedly.works/binding-uid"
	SubjectUIDAnn      = "kubernetes-policy.agentplane.allegedly.works/subject-uid"

	setsIndex    = "spec.policySets"
	subjectIndex = "spec.subject"
)

// Reconciler converges one PolicyBinding at a time: its native RoleBindings and ClusterRoleBindings,
// and its KubernetesReady condition.
//
// +kubebuilder:object:generate=false
type Reconciler struct {
	client.Client
	// Uncached: reading roles through the cache would watch every Role in the cluster.
	Roles             client.Reader
	PolicyNamespace   string
	SubjectNamespaces []string
}

// native is one binding to keep; a ClusterRoleBinding when Namespace is empty.
//
// +kubebuilder:object:generate=false
type native struct {
	Namespace, Name string
	Role            RoleRef
}

func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (ctrl.Result, error) {
	var binding PolicyBinding
	if err := r.Get(ctx, req.NamespacedName, &binding); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, r.converge(ctx, req.Name, nil)
		}
		return ctrl.Result{}, err
	}
	desired, reason, message, err := r.expand(ctx, &binding)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := r.converge(ctx, binding.Name, desired); err != nil {
		reason, message = "ApplyFailed", err.Error()
	}
	if err := r.applyCondition(ctx, &binding, reason, message); err != nil {
		return ctrl.Result{}, err
	}
	if reason != "Expired" && binding.Spec.ExpiresAt != nil {
		// No event marks the deadline, so the reconcile asks to be run again at it.
		return ctrl.Result{RequeueAfter: time.Until(binding.Spec.ExpiresAt.Time)}, nil
	}
	return ctrl.Result{}, nil
}

// expand returns the native bindings the binding grants, with the condition reason and message.
func (r *Reconciler) expand(ctx context.Context, binding *PolicyBinding) (map[native]client.Object, string, string, error) {
	spec := binding.Spec
	if spec.ExpiresAt != nil && !spec.ExpiresAt.After(time.Now()) {
		return nil, "Expired", "expired at " + spec.ExpiresAt.Format(time.RFC3339), nil
	}
	if !slices.Contains(r.SubjectNamespaces, spec.Subject.Namespace) {
		return nil, "SubjectNotWatched", fmt.Sprintf("subject namespace %s is not one this controller binds", spec.Subject.Namespace), nil
	}
	var account corev1.ServiceAccount
	if err := r.Get(ctx, types.NamespacedName(spec.Subject), &account); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, "SubjectMissing", fmt.Sprintf("ServiceAccount %s/%s not found", spec.Subject.Namespace, spec.Subject.Name), nil
		}
		return nil, "", "", err
	}
	desired := map[native]client.Object{}
	var problems []string
	for _, setName := range spec.PolicySets {
		var set PolicySet
		if err := r.Get(ctx, types.NamespacedName{Namespace: r.PolicyNamespace, Name: setName}, &set); err != nil {
			if !apierrors.IsNotFound(err) {
				return nil, "", "", err
			}
			problems = append(problems, "missing PolicySet "+setName)
			continue
		}
		for _, rule := range set.Spec.Rules {
			for _, n := range scopes(rule) {
				if err := r.requireRole(ctx, n); err != nil {
					problems = append(problems, err.Error())
					continue
				}
				desired[n] = r.render(binding, &account, n)
			}
		}
	}
	if len(problems) > 0 {
		return desired, "ApplyFailed", strings.Join(problems, "; "), nil
	}
	return desired, "Applied", fmt.Sprintf("%d native bindings", len(desired)), nil
}

func scopes(rule Rule) []native {
	switch rule.Type {
	case "kubernetes_role_binding":
		var out []native
		for _, ns := range rule.Namespaces {
			out = append(out, native{Namespace: ns, Role: *rule.Role})
		}
		return out
	case "kubernetes_cluster_role_binding":
		return []native{{Role: RoleRef{Kind: "ClusterRole", Name: rule.ClusterRole}}}
	}
	return nil // Another reader's rule type.
}

func (r *Reconciler) requireRole(ctx context.Context, n native) error {
	var err error
	if n.Role.Kind == "Role" {
		err = r.Roles.Get(ctx, types.NamespacedName{Namespace: n.Namespace, Name: n.Role.Name}, &rbacv1.Role{})
	} else {
		err = r.Roles.Get(ctx, types.NamespacedName{Name: n.Role.Name}, &rbacv1.ClusterRole{})
	}
	if apierrors.IsNotFound(err) {
		return fmt.Errorf("%s %s not found", n.Role.Kind, n.Role.Name)
	}
	return err
}

func (r *Reconciler) render(binding *PolicyBinding, account *corev1.ServiceAccount, n native) client.Object {
	digest := sha256.Sum256(fmt.Appendf(nil, "%s/%s/%s/%s/%s", binding.UID, account.UID, n.Role.Kind, n.Role.Name, n.Namespace))
	meta := metav1.ObjectMeta{
		Name:      fmt.Sprintf("pb-%.40s-%s", binding.Name, hex.EncodeToString(digest[:])[:16]),
		Namespace: n.Namespace,
		Labels:    map[string]string{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace},
		Annotations: map[string]string{
			BindingAnnotation: binding.Name,
			BindingUIDAnn:     string(binding.UID),
			SubjectUIDAnn:     string(account.UID),
		},
	}
	roleRef := rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: n.Role.Kind, Name: n.Role.Name}
	subjects := []rbacv1.Subject{{Kind: "ServiceAccount", Namespace: account.Namespace, Name: account.Name}}
	if n.Namespace == "" {
		return &rbacv1.ClusterRoleBinding{ObjectMeta: meta, RoleRef: roleRef, Subjects: subjects}
	}
	return &rbacv1.RoleBinding{ObjectMeta: meta, RoleRef: roleRef, Subjects: subjects}
}

// converge makes the binding's labeled native bindings exactly `desired`, keyed by name.
func (r *Reconciler) converge(ctx context.Context, bindingName string, desired map[native]client.Object) error {
	want := map[string]client.Object{}
	for _, obj := range desired {
		want[obj.GetNamespace()+"/"+obj.GetName()] = obj
	}
	actual, err := r.natives(ctx, bindingName)
	if err != nil {
		return err
	}
	for key, obj := range actual {
		if wanted, ok := want[key]; ok && matches(obj, wanted) {
			delete(want, key)
			continue
		}
		// Not granted, or edited by hand: a roleRef cannot change in place.
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return err
		}
	}
	for _, obj := range want {
		if err := r.Create(ctx, obj); err != nil && !apierrors.IsAlreadyExists(err) {
			return err
		}
	}
	return nil
}

func (r *Reconciler) natives(ctx context.Context, bindingName string) (map[string]client.Object, error) {
	labels := client.MatchingLabels{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace}
	var roleBindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &roleBindings, labels); err != nil {
		return nil, err
	}
	if err := r.List(ctx, &clusterBindings, labels); err != nil {
		return nil, err
	}
	out := map[string]client.Object{}
	for i := range roleBindings.Items {
		out[roleBindings.Items[i].Namespace+"/"+roleBindings.Items[i].Name] = &roleBindings.Items[i]
	}
	for i := range clusterBindings.Items {
		out["/"+clusterBindings.Items[i].Name] = &clusterBindings.Items[i]
	}
	for key, obj := range out {
		if obj.GetAnnotations()[BindingAnnotation] != bindingName {
			delete(out, key)
		}
	}
	return out, nil
}

func matches(actual, wanted client.Object) bool {
	roleRef := func(obj client.Object) (rbacv1.RoleRef, []rbacv1.Subject) {
		if rb, ok := obj.(*rbacv1.RoleBinding); ok {
			return rb.RoleRef, rb.Subjects
		}
		crb := obj.(*rbacv1.ClusterRoleBinding)
		return crb.RoleRef, crb.Subjects
	}
	actualRef, actualSubjects := roleRef(actual)
	wantedRef, wantedSubjects := roleRef(wanted)
	return actualRef == wantedRef && slices.Equal(actualSubjects, wantedSubjects) &&
		actual.GetAnnotations()[BindingUIDAnn] == wanted.GetAnnotations()[BindingUIDAnn] &&
		actual.GetAnnotations()[SubjectUIDAnn] == wanted.GetAnnotations()[SubjectUIDAnn]
}

// applyCondition server-side applies only KubernetesReady, so other readers' conditions stay.
func (r *Reconciler) applyCondition(ctx context.Context, binding *PolicyBinding, reason, message string) error {
	status := metav1.ConditionFalse
	if reason == "Applied" {
		status = metav1.ConditionTrue
	}
	conditions := slices.Clone(binding.Status.Conditions)
	if !meta.SetStatusCondition(&conditions, metav1.Condition{
		Type: KubernetesReady, Status: status, Reason: reason, Message: message, ObservedGeneration: binding.Generation,
	}) {
		return nil
	}
	patch := &PolicyBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: GroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: binding.Name, Namespace: binding.Namespace},
		Status:     Status{Conditions: []metav1.Condition{*meta.FindStatusCondition(conditions, KubernetesReady)}},
	}
	// The CRD has no generated apply configuration; an unstructured one carries the same fields.
	object, err := runtime.DefaultUnstructuredConverter.ToUnstructured(patch)
	if err != nil {
		return err
	}
	return r.Status().Apply(ctx, client.ApplyConfigurationFromUnstructured(&unstructured.Unstructured{Object: object}),
		client.FieldOwner(FieldManager), client.ForceOwnership)
}

// SetupWithManager wires the watches: each PolicyBinding, plus whatever it reads mapped back to it.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	ctx := context.Background()
	indexer := mgr.GetFieldIndexer()
	if err := indexer.IndexField(ctx, &PolicyBinding{}, setsIndex, func(obj client.Object) []string {
		return obj.(*PolicyBinding).Spec.PolicySets
	}); err != nil {
		return err
	}
	if err := indexer.IndexField(ctx, &PolicyBinding{}, subjectIndex, func(obj client.Object) []string {
		subject := obj.(*PolicyBinding).Spec.Subject
		return []string{subject.Namespace + "/" + subject.Name}
	}); err != nil {
		return err
	}
	byIndex := func(index string, value func(client.Object) string) handler.EventHandler {
		return handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
			var bindings PolicyBindingList
			if err := r.List(ctx, &bindings, client.InNamespace(r.PolicyNamespace), client.MatchingFields{index: value(obj)}); err != nil {
				return nil
			}
			var out []reconcile.Request
			for _, b := range bindings.Items {
				out = append(out, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&b)})
			}
			return out
		})
	}
	// A native binding maps back by annotation, including to a PolicyBinding deleted while the
	// controller was down: the initial list enqueues it, and its NotFound reconcile deletes them.
	toBinding := handler.EnqueueRequestsFromMapFunc(func(_ context.Context, obj client.Object) []reconcile.Request {
		name, ok := obj.GetAnnotations()[BindingAnnotation]
		if !ok {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: r.PolicyNamespace, Name: name}}}
	})
	return ctrl.NewControllerManagedBy(mgr).
		For(&PolicyBinding{}).
		Watches(&PolicySet{}, byIndex(setsIndex, client.Object.GetName)).
		Watches(&corev1.ServiceAccount{}, byIndex(subjectIndex, func(obj client.Object) string {
			return obj.GetNamespace() + "/" + obj.GetName()
		})).
		Watches(&rbacv1.RoleBinding{}, toBinding).
		Watches(&rbacv1.ClusterRoleBinding{}, toBinding).
		Complete(r)
}
