package policycontroller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
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
	applyrbacv1 "k8s.io/client-go/applyconfigurations/rbac/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

const (
	KubernetesReady    = "KubernetesReady"
	FieldManager       = "agentplane-policy-controller"
	ManagedByLabel     = "app.kubernetes.io/managed-by"
	PolicyNamespaceLbl = "policy.agentplane.allegedly.works/policy-namespace"
	BindingAnnotation  = "policy.agentplane.allegedly.works/binding"
	BindingUIDAnn      = "policy.agentplane.allegedly.works/binding-uid"
	SubjectUIDAnn      = "policy.agentplane.allegedly.works/subject-uid"

	setsIndex          = "spec.policySets"
	subjectIndex       = "spec.subject"
	nativeBindingIndex = "metadata.annotations[" + BindingAnnotation + "]"
	// Bindings told a role is missing; any role change retries them.
	roleMissingIndex = "status.roleMissing"
)

// Kubernetes accepts a binding to a role that does not exist; this controller does not create one.
var errRoleMissing = errors.New("role not found")

// Reconciler converges one PolicyBinding at a time: its native RoleBindings and ClusterRoleBindings,
// and its KubernetesReady condition.
//
// +kubebuilder:object:generate=false
type Reconciler struct {
	client.Client
	// Uncached: reading roles or checking desired-name collisions through the cache would watch
	// every Role and foreign binding in the cluster.
	APIReader         client.Reader
	PolicyNamespace   string
	SubjectNamespaces []string
}

// native is one binding to keep; a ClusterRoleBinding when Namespace is empty.
//
// +kubebuilder:object:generate=false
type native struct {
	Namespace, Name string
	Role            policyapi.RoleRef
}

func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (result ctrl.Result, err error) {
	var binding policyapi.PolicyBinding
	if err := r.Get(ctx, req.NamespacedName, &binding); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, r.converge(ctx, req.Name, nil)
		}
		return ctrl.Result{}, err
	}
	if expiresAt := binding.Spec.ExpiresAt; expiresAt != nil {
		if remaining := time.Until(expiresAt.Time); remaining > 0 {
			// No event marks the deadline, so a successful reconcile asks to be run again at it (a
			// failed one is retried with backoff anyway).
			defer func() {
				if err == nil {
					result.RequeueAfter = remaining
				}
			}()
		} else {
			// Expiry is deletion for every rule type; the NotFound reconcile that follows removes the
			// native bindings.
			return ctrl.Result{}, client.IgnoreNotFound(r.Delete(ctx, &binding, client.Preconditions{UID: &binding.UID}))
		}
	}
	desired, reason, message, err := r.expand(ctx, &binding)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := r.converge(ctx, binding.Name, desired); err != nil {
		reason, message = "ApplyFailed", err.Error()
		return ctrl.Result{}, errors.Join(err, r.applyCondition(ctx, &binding, reason, message))
	}
	return ctrl.Result{}, r.applyCondition(ctx, &binding, reason, message)
}

// expand returns the native bindings the binding grants, with the condition reason and message.
func (r *Reconciler) expand(ctx context.Context, binding *policyapi.PolicyBinding) (map[native]client.Object, string, string, error) {
	spec := binding.Spec
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
	var missingSets, missingRoles []string
	for _, setName := range spec.PolicySets {
		var set policyapi.PolicySet
		if err := r.Get(ctx, types.NamespacedName{Namespace: r.PolicyNamespace, Name: setName}, &set); err != nil {
			if !apierrors.IsNotFound(err) {
				return nil, "", "", err
			}
			// The binding's other sets still grant.
			missingSets = append(missingSets, setName)
			continue
		}
		for _, rule := range set.Spec.Rules {
			for _, n := range scopes(rule) {
				if err := r.requireRole(ctx, n); err != nil {
					if !errors.Is(err, errRoleMissing) {
						return nil, "", "", err
					}
					missingRoles = append(missingRoles, err.Error())
					continue
				}
				desired[n] = r.render(binding, &account, n)
			}
		}
	}
	if len(missingRoles) > 0 {
		return desired, "ApplyFailed", strings.Join(missingRoles, "; "), nil
	}
	if len(missingSets) > 0 {
		return desired, policyapi.PolicySetUnavailable, "missing PolicySets: " + strings.Join(missingSets, ", "), nil
	}
	return desired, "Applied", fmt.Sprintf("%d native bindings", len(desired)), nil
}

func scopes(rule policyapi.Rule) []native {
	switch rule.Type {
	case "kubernetes_role_binding":
		var out []native
		for _, ns := range rule.Namespaces {
			out = append(out, native{Namespace: ns, Role: *rule.Role})
		}
		return out
	case "kubernetes_cluster_role_binding":
		return []native{{Role: policyapi.RoleRef{Kind: "ClusterRole", Name: rule.ClusterRole}}}
	}
	return nil // Another reader's rule type.
}

func (r *Reconciler) requireRole(ctx context.Context, n native) error {
	var err error
	if n.Role.Kind == "Role" {
		err = r.APIReader.Get(ctx, types.NamespacedName{Namespace: n.Namespace, Name: n.Role.Name}, &rbacv1.Role{})
	} else {
		err = r.APIReader.Get(ctx, types.NamespacedName{Name: n.Role.Name}, &rbacv1.ClusterRole{})
	}
	if apierrors.IsNotFound(err) {
		return fmt.Errorf("%w: %s %s", errRoleMissing, n.Role.Kind, n.Role.Name)
	}
	return err
}

func (r *Reconciler) render(binding *policyapi.PolicyBinding, account *corev1.ServiceAccount, n native) client.Object {
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
	want := map[client.ObjectKey]client.Object{}
	for _, obj := range desired {
		want[client.ObjectKeyFromObject(obj)] = obj
	}
	actual, err := r.natives(ctx, bindingName)
	if err != nil {
		return err
	}
	for key, obj := range actual {
		if wanted, ok := want[key]; ok {
			if sameIdentity(obj, wanted) {
				if !sameRoleRef(obj, wanted) {
					if err := r.deleteNative(ctx, obj); err != nil {
						return err
					}
					return fmt.Errorf("deleted %T %s to replace its immutable roleRef; retry to create the replacement", obj, client.ObjectKeyFromObject(obj))
				}
				if matches(obj, wanted) {
					delete(want, key)
				}
				continue
			}
		}
		// Revoked or stale identity grants must stop applying immediately. A roleRef is immutable,
		// so its managed binding is replaced below with the desired role.
		if err := r.deleteNative(ctx, obj); err != nil {
			return err
		}
	}
	for _, obj := range want {
		if err := r.applyNative(ctx, obj); err != nil {
			return err
		}
	}
	return nil
}

func (r *Reconciler) natives(ctx context.Context, bindingName string) (map[client.ObjectKey]client.Object, error) {
	labels := client.MatchingLabels{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace}
	index := client.MatchingFields{nativeBindingIndex: bindingName}
	var roleBindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &roleBindings, labels, index); err != nil {
		return nil, err
	}
	if err := r.List(ctx, &clusterBindings, labels, index); err != nil {
		return nil, err
	}
	out := map[client.ObjectKey]client.Object{}
	for i := range roleBindings.Items {
		out[client.ObjectKeyFromObject(&roleBindings.Items[i])] = &roleBindings.Items[i]
	}
	for i := range clusterBindings.Items {
		out[client.ObjectKeyFromObject(&clusterBindings.Items[i])] = &clusterBindings.Items[i]
	}
	return out, nil
}

func matches(actual, wanted client.Object) bool {
	for key, value := range wanted.GetLabels() {
		if actual.GetLabels()[key] != value {
			return false
		}
	}
	for key, value := range wanted.GetAnnotations() {
		if actual.GetAnnotations()[key] != value {
			return false
		}
	}
	actualRef, actualSubjects := nativeRoleAndSubjects(actual)
	wantedRef, wantedSubjects := nativeRoleAndSubjects(wanted)
	return actualRef == wantedRef && slices.Equal(actualSubjects, wantedSubjects)
}

func sameIdentity(actual, wanted client.Object) bool {
	return actual.GetAnnotations()[BindingUIDAnn] == wanted.GetAnnotations()[BindingUIDAnn] &&
		actual.GetAnnotations()[SubjectUIDAnn] == wanted.GetAnnotations()[SubjectUIDAnn]
}

func sameRoleRef(actual, wanted client.Object) bool {
	actualRef, _ := nativeRoleAndSubjects(actual)
	wantedRef, _ := nativeRoleAndSubjects(wanted)
	return actualRef == wantedRef
}

func nativeRoleAndSubjects(obj client.Object) (rbacv1.RoleRef, []rbacv1.Subject) {
	if rb, ok := obj.(*rbacv1.RoleBinding); ok {
		return rb.RoleRef, rb.Subjects
	}
	crb := obj.(*rbacv1.ClusterRoleBinding)
	return crb.RoleRef, crb.Subjects
}

func (r *Reconciler) deleteNative(ctx context.Context, obj client.Object) error {
	var options []client.DeleteOption
	if uid := obj.GetUID(); uid != "" {
		options = append(options, client.Preconditions{UID: &uid})
	}
	return client.IgnoreNotFound(r.Delete(ctx, obj, options...))
}

func (r *Reconciler) applyNative(ctx context.Context, wanted client.Object) error {
	existing := wanted.DeepCopyObject().(client.Object)
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(wanted), existing); err != nil {
		if !apierrors.IsNotFound(err) {
			return err
		}
		// Claim the name with an exclusive create before applying it. If another actor wins after
		// the read, AlreadyExists returns an error; the next reconcile checks the winner uncached.
		existing = wanted.DeepCopyObject().(client.Object)
		if err := r.Create(ctx, existing); err != nil {
			return fmt.Errorf("claiming native binding name %s: %w", client.ObjectKeyFromObject(wanted), err)
		}
	} else {
		if !matchesIdentity(existing, wanted) {
			return fmt.Errorf("refusing to adopt foreign %T %s", existing, client.ObjectKeyFromObject(wanted))
		}
		if !sameRoleRef(existing, wanted) {
			// roleRef is immutable, so remove the old managed object before applying its replacement.
			if err := r.deleteNative(ctx, existing); err != nil {
				return err
			}
			return fmt.Errorf("deleted %T %s to replace its immutable roleRef; retry to create the replacement", existing, client.ObjectKeyFromObject(wanted))
		}
	}

	var configuration any
	switch obj := wanted.(type) {
	case *rbacv1.RoleBinding:
		applyConfiguration := applyrbacv1.RoleBinding(obj.Name, obj.Namespace).
			WithLabels(obj.Labels).
			WithAnnotations(obj.Annotations).
			WithRoleRef(applyrbacv1.RoleRef().WithAPIGroup(obj.RoleRef.APIGroup).WithKind(obj.RoleRef.Kind).WithName(obj.RoleRef.Name)).
			WithSubjects(applyrbacv1.Subject().WithKind("ServiceAccount").WithNamespace(obj.Subjects[0].Namespace).WithName(obj.Subjects[0].Name))
		if existing.GetUID() != "" {
			applyConfiguration.WithUID(existing.GetUID()).WithResourceVersion(existing.GetResourceVersion())
		}
		configuration = applyConfiguration
	case *rbacv1.ClusterRoleBinding:
		applyConfiguration := applyrbacv1.ClusterRoleBinding(obj.Name).
			WithLabels(obj.Labels).
			WithAnnotations(obj.Annotations).
			WithRoleRef(applyrbacv1.RoleRef().WithAPIGroup(obj.RoleRef.APIGroup).WithKind(obj.RoleRef.Kind).WithName(obj.RoleRef.Name)).
			WithSubjects(applyrbacv1.Subject().WithKind("ServiceAccount").WithNamespace(obj.Subjects[0].Namespace).WithName(obj.Subjects[0].Name))
		if existing.GetUID() != "" {
			applyConfiguration.WithUID(existing.GetUID()).WithResourceVersion(existing.GetResourceVersion())
		}
		configuration = applyConfiguration
	default:
		return fmt.Errorf("unsupported native binding type %T", wanted)
	}
	object, err := runtime.DefaultUnstructuredConverter.ToUnstructured(configuration)
	if err != nil {
		return err
	}
	return r.Apply(ctx, client.ApplyConfigurationFromUnstructured(&unstructured.Unstructured{Object: object}),
		client.FieldOwner(FieldManager), client.ForceOwnership)
}

func matchesIdentity(actual, wanted client.Object) bool {
	for key, value := range wanted.GetLabels() {
		if actual.GetLabels()[key] != value {
			return false
		}
	}
	for key, value := range wanted.GetAnnotations() {
		if actual.GetAnnotations()[key] != value {
			return false
		}
	}
	return true
}

// applyCondition server-side applies only KubernetesReady, so other readers' conditions stay.
func (r *Reconciler) applyCondition(ctx context.Context, binding *policyapi.PolicyBinding, reason, message string) error {
	status := metav1.ConditionFalse
	if reason == "Applied" {
		status = metav1.ConditionTrue
	}
	patch := binding.PrepareConditionApply(metav1.Condition{Type: KubernetesReady, Status: status, Reason: reason, Message: message})
	if patch == nil {
		return nil
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
	indexNativeBinding := func(obj client.Object) []string {
		if bindingName := obj.GetAnnotations()[BindingAnnotation]; bindingName != "" {
			return []string{bindingName}
		}
		return nil
	}
	if err := indexer.IndexField(ctx, &rbacv1.RoleBinding{}, nativeBindingIndex, indexNativeBinding); err != nil {
		return err
	}
	if err := indexer.IndexField(ctx, &rbacv1.ClusterRoleBinding{}, nativeBindingIndex, indexNativeBinding); err != nil {
		return err
	}
	if err := indexer.IndexField(ctx, &policyapi.PolicyBinding{}, setsIndex, func(obj client.Object) []string {
		return obj.(*policyapi.PolicyBinding).Spec.PolicySets
	}); err != nil {
		return err
	}
	if err := indexer.IndexField(ctx, &policyapi.PolicyBinding{}, subjectIndex, func(obj client.Object) []string {
		subject := obj.(*policyapi.PolicyBinding).Spec.Subject
		return []string{subject.Namespace + "/" + subject.Name}
	}); err != nil {
		return err
	}
	if err := indexer.IndexField(ctx, &policyapi.PolicyBinding{}, roleMissingIndex, func(obj client.Object) []string {
		condition := meta.FindStatusCondition(obj.(*policyapi.PolicyBinding).Status.Conditions, KubernetesReady)
		if condition != nil && condition.Reason == "ApplyFailed" {
			return []string{"true"}
		}
		return nil
	}); err != nil {
		return err
	}
	byIndex := func(index string, value func(client.Object) string) handler.EventHandler {
		return handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
			var bindings policyapi.PolicyBindingList
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
		For(&policyapi.PolicyBinding{}).
		Watches(&policyapi.PolicySet{}, byIndex(setsIndex, client.Object.GetName)).
		Watches(&corev1.ServiceAccount{}, byIndex(subjectIndex, func(obj client.Object) string {
			return obj.GetNamespace() + "/" + obj.GetName()
		})).
		Watches(&rbacv1.RoleBinding{}, toBinding).
		Watches(&rbacv1.ClusterRoleBinding{}, toBinding).
		// Metadata only: whether a role exists is all a binding depends on.
		WatchesMetadata(&rbacv1.Role{}, byIndex(roleMissingIndex, func(client.Object) string { return "true" })).
		WatchesMetadata(&rbacv1.ClusterRole{}, byIndex(roleMissingIndex, func(client.Object) string { return "true" })).
		Complete(r)
}
