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
	apimeta "k8s.io/apimachinery/pkg/api/meta"
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
	policyapply "github.com/agentydragon/ducktape/agentplane/policy/client/apply"
	policyapplyv1 "github.com/agentydragon/ducktape/agentplane/policy/client/applyconfigurations/api/v1alpha1"
)

const (
	KubernetesReady    = "KubernetesReady"
	FieldManager       = "agentplane-policy-controller"
	ManagedByLabel     = "app.kubernetes.io/managed-by"
	PolicyNamespaceLbl = "policy.agentplane.allegedly.works/policy-namespace"
	BindingAnnotation  = "policy.agentplane.allegedly.works/binding"
	BindingUIDAnn      = "policy.agentplane.allegedly.works/binding-uid"
	SubjectUIDAnn      = "policy.agentplane.allegedly.works/subject-uid"
	GrantAnnotation    = "policy.agentplane.allegedly.works/account-grant"
	GrantUIDAnn        = "policy.agentplane.allegedly.works/account-grant-uid"
	// On a ServiceAccount the controller created: the ManagedServiceAccount it was created for, and
	// that account's UID, which is the ownership proof.
	ManagedAccountAnnotation = "policy.agentplane.allegedly.works/managed-service-account"
	ManagedAccountUIDLabel   = "policy.agentplane.allegedly.works/managed-service-account-uid"
	// LocalAPIServer names the cluster the controller runs in, the only apiserver it reaches.
	LocalAPIServer = "local"

	setsIndex          = "spec.policySets"
	subjectIndex       = "spec.subject"
	roleSetsIndex      = "spec.roleSets"
	accountIndex       = "spec.account"
	nativeBindingIndex = "metadata.annotations[" + BindingAnnotation + "]"
	nativeGrantIndex   = "metadata.annotations[" + GrantAnnotation + "]"
	managedAccountIdx  = "metadata.annotations[" + ManagedAccountAnnotation + "]"
	// Bindings and grants by each role in status.missingRoles; that role's creation retries them.
	missingRoleIndex = "status.missingRoles"
)

// owner is a kind native bindings are kept for: the annotations naming the owning object on each
// native binding, the field index over the name annotation, and the native binding name prefix.
//
// +kubebuilder:object:generate=false
type owner struct{ nameAnn, uidAnn, index, prefix string }

var (
	bindingOwner = owner{nameAnn: BindingAnnotation, uidAnn: BindingUIDAnn, index: nativeBindingIndex, prefix: "pb"}
	grantOwner   = owner{nameAnn: GrantAnnotation, uidAnn: GrantUIDAnn, index: nativeGrantIndex, prefix: "kag"}
)

// Reconciler converges one PolicyBinding or KubernetesAccountGrant at a time: its native
// RoleBindings and ClusterRoleBindings, and its KubernetesReady condition. It also keeps each
// ManagedServiceAccount's ServiceAccount.
//
// +kubebuilder:object:generate=false
type Reconciler struct {
	client.Client
	// Uncached: reading roles or checking desired-name collisions through the cache would watch
	// every Role and foreign binding in the cluster.
	APIReader         client.Reader
	PolicyNamespace   string
	SubjectNamespaces []string
	// Where ManagedServiceAccounts' ServiceAccounts are created. Grants may name accounts here as
	// well as in SubjectNamespaces.
	AccountsNamespace string
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
			return ctrl.Result{}, r.converge(ctx, bindingOwner, req.Name, nil)
		}
		return ctrl.Result{}, err
	}
	// Expiry is deletion for every rule type; the NotFound reconcile that follows removes the
	// native bindings.
	requeue, expired, err := r.expire(ctx, &binding, binding.Spec.ExpiresAt)
	if expired || err != nil {
		return ctrl.Result{}, err
	}
	defer func() {
		if err == nil {
			result.RequeueAfter = requeue
		}
	}()
	e, err := r.expand(ctx, &binding)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := r.converge(ctx, bindingOwner, binding.Name, e.desired); err != nil {
		e.reason, e.message = "ApplyFailed", err.Error()
		return ctrl.Result{}, errors.Join(err, r.applyStatus(ctx, &binding, e))
	}
	return ctrl.Result{}, r.applyStatus(ctx, &binding, e)
}

// expire deletes obj once expiresAt has passed, reporting expired. Before then it returns how long
// remains: no event marks the deadline, so a successful reconcile asks to be run again at it (a
// failed one is retried with backoff anyway). Without expiresAt it returns zero.
func (r *Reconciler) expire(ctx context.Context, obj client.Object, expiresAt *metav1.Time) (time.Duration, bool, error) {
	if expiresAt == nil {
		return 0, false, nil
	}
	if remaining := time.Until(expiresAt.Time); remaining > 0 {
		return remaining, false, nil
	}
	uid := obj.GetUID()
	return 0, true, client.IgnoreNotFound(r.Delete(ctx, obj, client.Preconditions{UID: &uid}))
}

// expansion is what a binding or grant grants and what its status reports.
//
// +kubebuilder:object:generate=false
type expansion struct {
	desired map[native]client.Object
	// Sorted by missingRoleKey, so an unchanged expansion writes no status.
	missingRoles    []policyapi.MissingRole
	reason, message string
}

func (r *Reconciler) expand(ctx context.Context, binding *policyapi.PolicyBinding) (expansion, error) {
	spec := binding.Spec
	account, e, err := r.subject(ctx, spec.Subject.Namespace, spec.Subject.Name, r.SubjectNamespaces)
	if account == nil {
		return e, err
	}
	var natives []native
	missing := unavailableSets{reason: policyapi.PolicySetUnavailable, kind: "PolicySets"}
	for _, setName := range spec.PolicySets {
		var set policyapi.PolicySet
		if err := r.Get(ctx, types.NamespacedName{Namespace: r.PolicyNamespace, Name: setName}, &set); err != nil {
			if !apierrors.IsNotFound(err) {
				return expansion{}, err
			}
			// The binding's other sets still grant.
			missing.names = append(missing.names, setName)
			continue
		}
		for _, rule := range set.Spec.Rules {
			natives = append(natives, scopes(rule)...)
		}
	}
	return r.expandNatives(ctx, bindingOwner, binding, account, natives, missing)
}

// subject is the ServiceAccount a binding or grant names, or, when there is none to bind, the
// expansion reporting why.
func (r *Reconciler) subject(ctx context.Context, namespace, name string, allowed []string) (*corev1.ServiceAccount, expansion, error) {
	if !slices.Contains(allowed, namespace) {
		return nil, expansion{reason: "SubjectNotWatched", message: fmt.Sprintf("subject namespace %s is not one this controller binds", namespace)}, nil
	}
	var account corev1.ServiceAccount
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &account); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, expansion{reason: "SubjectMissing", message: fmt.Sprintf("ServiceAccount %s/%s not found", namespace, name)}, nil
		}
		return nil, expansion{}, err
	}
	return &account, expansion{}, nil
}

// unavailableSets are the sets an owner names that do not exist, and how its condition reports them.
//
// +kubebuilder:object:generate=false
type unavailableSets struct {
	reason, kind string
	names        []string
}

// expandNatives renders the native bindings for natives whose role exists and reports the rest.
func (r *Reconciler) expandNatives(ctx context.Context, o owner, obj metav1.Object, account *corev1.ServiceAccount, natives []native, missing unavailableSets) (expansion, error) {
	e := expansion{desired: map[native]client.Object{}}
	for _, n := range natives {
		role := requiredRole(n)
		found, err := r.roleExists(ctx, role)
		if err != nil {
			return expansion{}, err
		}
		if !found {
			if !slices.Contains(e.missingRoles, role) {
				e.missingRoles = append(e.missingRoles, role)
			}
			continue
		}
		e.desired[n] = r.render(o, obj, account, n)
	}
	switch {
	case len(e.missingRoles) > 0:
		slices.SortFunc(e.missingRoles, func(a, b policyapi.MissingRole) int {
			return strings.Compare(missingRoleKey(a), missingRoleKey(b))
		})
		var names []string
		for _, role := range e.missingRoles {
			names = append(names, missingRoleKey(role))
		}
		e.reason, e.message = "RoleMissing", "missing roles: "+strings.Join(names, ", ")
	case len(missing.names) > 0:
		e.reason, e.message = missing.reason, "missing "+missing.kind+": "+strings.Join(missing.names, ", ")
	default:
		e.reason, e.message = "Applied", fmt.Sprintf("%d native bindings", len(e.desired))
	}
	return e, nil
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

// requiredRole is the role a native binding refers to; a ClusterRole has no namespace even when a
// RoleBinding grants it.
func requiredRole(n native) policyapi.MissingRole {
	role := policyapi.MissingRole{Kind: n.Role.Kind, Name: n.Role.Name}
	if n.Role.Kind == "Role" {
		role.Namespace = n.Namespace
	}
	return role
}

// missingRoleKey names a role in missingRoleIndex and in condition messages.
func missingRoleKey(role policyapi.MissingRole) string {
	if role.Namespace == "" {
		return role.Kind + " " + role.Name
	}
	return role.Kind + " " + role.Namespace + "/" + role.Name
}

// Kubernetes accepts a binding to a role that does not exist; this controller does not create one.
func (r *Reconciler) roleExists(ctx context.Context, role policyapi.MissingRole) (bool, error) {
	var err error
	if role.Kind == "Role" {
		err = r.APIReader.Get(ctx, types.NamespacedName{Namespace: role.Namespace, Name: role.Name}, &rbacv1.Role{})
	} else {
		err = r.APIReader.Get(ctx, types.NamespacedName{Name: role.Name}, &rbacv1.ClusterRole{})
	}
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

func (r *Reconciler) render(o owner, obj metav1.Object, account *corev1.ServiceAccount, n native) client.Object {
	digest := sha256.Sum256(fmt.Appendf(nil, "%s/%s/%s/%s/%s", obj.GetUID(), account.UID, n.Role.Kind, n.Role.Name, n.Namespace))
	meta := metav1.ObjectMeta{
		Name:      fmt.Sprintf("%s-%.40s-%s", o.prefix, obj.GetName(), hex.EncodeToString(digest[:])[:16]),
		Namespace: n.Namespace,
		Labels:    map[string]string{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace},
		Annotations: map[string]string{
			o.nameAnn:     obj.GetName(),
			o.uidAnn:      string(obj.GetUID()),
			SubjectUIDAnn: string(account.UID),
		},
	}
	roleRef := rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: n.Role.Kind, Name: n.Role.Name}
	subjects := []rbacv1.Subject{{Kind: "ServiceAccount", Namespace: account.Namespace, Name: account.Name}}
	if n.Namespace == "" {
		return &rbacv1.ClusterRoleBinding{ObjectMeta: meta, RoleRef: roleRef, Subjects: subjects}
	}
	return &rbacv1.RoleBinding{ObjectMeta: meta, RoleRef: roleRef, Subjects: subjects}
}

// converge makes the owner's labeled native bindings exactly `desired`, keyed by name.
func (r *Reconciler) converge(ctx context.Context, o owner, ownerName string, desired map[native]client.Object) error {
	want := map[client.ObjectKey]client.Object{}
	for _, obj := range desired {
		want[client.ObjectKeyFromObject(obj)] = obj
	}
	actual, err := r.natives(ctx, o, ownerName)
	if err != nil {
		return err
	}
	for key, obj := range actual {
		if wanted, ok := want[key]; ok {
			if sameIdentity(o, obj, wanted) {
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

func (r *Reconciler) natives(ctx context.Context, o owner, ownerName string) (map[client.ObjectKey]client.Object, error) {
	labels := client.MatchingLabels{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace}
	index := client.MatchingFields{o.index: ownerName}
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

func sameIdentity(o owner, actual, wanted client.Object) bool {
	return actual.GetAnnotations()[o.uidAnn] == wanted.GetAnnotations()[o.uidAnn] &&
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

// applyStatus server-side applies the status this controller owns, KubernetesReady and
// missingRoles, in one configuration; other readers' conditions stay.
func (r *Reconciler) applyStatus(ctx context.Context, binding *policyapi.PolicyBinding, e expansion) error {
	configuration, changed := policyapply.ConditionApply(binding, e.condition())
	if !changed && slices.Equal(binding.Status.MissingRoles, e.missingRoles) {
		return nil
	}
	configuration.Status.WithMissingRoles(e.missingRoleConfigurations()...)
	return r.Status().Apply(ctx, configuration,
		client.FieldOwner(FieldManager), client.ForceOwnership)
}

func (e expansion) condition() metav1.Condition {
	status := metav1.ConditionFalse
	if e.reason == "Applied" {
		status = metav1.ConditionTrue
	}
	return metav1.Condition{Type: KubernetesReady, Status: status, Reason: e.reason, Message: e.message}
}

func (e expansion) missingRoleConfigurations() []*policyapplyv1.MissingRoleApplyConfiguration {
	var out []*policyapplyv1.MissingRoleApplyConfiguration
	for _, role := range e.missingRoles {
		missing := policyapplyv1.MissingRole().WithKind(role.Kind).WithName(role.Name)
		if role.Namespace != "" {
			missing.WithNamespace(role.Namespace)
		}
		out = append(out, missing)
	}
	return out
}

// IndexFields registers the field indexes the reconcilers list by.
func (r *Reconciler) IndexFields(ctx context.Context, indexer client.FieldIndexer) error {
	byAnnotation := func(key string) client.IndexerFunc {
		return func(obj client.Object) []string {
			if name := obj.GetAnnotations()[key]; name != "" {
				return []string{name}
			}
			return nil
		}
	}
	missingRoles := func(roles []policyapi.MissingRole) []string {
		var out []string
		for _, role := range roles {
			out = append(out, missingRoleKey(role))
		}
		return out
	}
	for _, index := range []struct {
		obj   client.Object
		field string
		value client.IndexerFunc
	}{
		{&rbacv1.RoleBinding{}, nativeBindingIndex, byAnnotation(BindingAnnotation)},
		{&rbacv1.ClusterRoleBinding{}, nativeBindingIndex, byAnnotation(BindingAnnotation)},
		{&rbacv1.RoleBinding{}, nativeGrantIndex, byAnnotation(GrantAnnotation)},
		{&rbacv1.ClusterRoleBinding{}, nativeGrantIndex, byAnnotation(GrantAnnotation)},
		{&corev1.ServiceAccount{}, managedAccountIdx, byAnnotation(ManagedAccountAnnotation)},
		{&policyapi.PolicyBinding{}, setsIndex, func(obj client.Object) []string {
			return obj.(*policyapi.PolicyBinding).Spec.PolicySets
		}},
		{&policyapi.PolicyBinding{}, subjectIndex, func(obj client.Object) []string {
			subject := obj.(*policyapi.PolicyBinding).Spec.Subject
			return []string{subject.Namespace + "/" + subject.Name}
		}},
		{&policyapi.PolicyBinding{}, missingRoleIndex, func(obj client.Object) []string {
			return missingRoles(obj.(*policyapi.PolicyBinding).Status.MissingRoles)
		}},
		{&policyapi.KubernetesAccountGrant{}, roleSetsIndex, func(obj client.Object) []string {
			return obj.(*policyapi.KubernetesAccountGrant).Spec.RoleSets
		}},
		{&policyapi.KubernetesAccountGrant{}, accountIndex, func(obj client.Object) []string {
			account := obj.(*policyapi.KubernetesAccountGrant).Spec.Account
			if account.APIServer != LocalAPIServer {
				return nil
			}
			return []string{account.Namespace + "/" + account.Name}
		}},
		{&policyapi.KubernetesAccountGrant{}, missingRoleIndex, func(obj client.Object) []string {
			return missingRoles(obj.(*policyapi.KubernetesAccountGrant).Status.MissingRoles)
		}},
	} {
		if err := indexer.IndexField(ctx, index.obj, index.field, index.value); err != nil {
			return err
		}
	}
	return nil
}

// enqueueByIndex maps an object to the policy-namespace objects of list type L whose index holds
// value(obj).
func enqueueByIndex[T any, L interface {
	*T
	client.ObjectList
}](r *Reconciler, index string, value func(client.Object) string) handler.EventHandler {
	return handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		list := L(new(T))
		if err := r.List(ctx, list, client.InNamespace(r.PolicyNamespace), client.MatchingFields{index: value(obj)}); err != nil {
			ctrl.LoggerFrom(ctx).Error(err, "listing objects to reconcile", "index", index)
			return nil
		}
		items, err := apimeta.ExtractList(list)
		if err != nil {
			ctrl.LoggerFrom(ctx).Error(err, "listing objects to reconcile", "index", index)
			return nil
		}
		var out []reconcile.Request
		for _, item := range items {
			out = append(out, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(item.(client.Object))})
		}
		return out
	})
}

// toOwner maps a native binding back to its owner by annotation, including to one deleted while the
// controller was down: the initial list enqueues it, and its NotFound reconcile deletes them.
func (r *Reconciler) toOwner(o owner) handler.EventHandler {
	return handler.EnqueueRequestsFromMapFunc(func(_ context.Context, obj client.Object) []reconcile.Request {
		name, ok := obj.GetAnnotations()[o.nameAnn]
		if !ok {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: r.PolicyNamespace, Name: name}}}
	})
}

func accountKey(obj client.Object) string { return obj.GetNamespace() + "/" + obj.GetName() }

func roleKey(obj client.Object) string {
	return missingRoleKey(policyapi.MissingRole{Kind: "Role", Namespace: obj.GetNamespace(), Name: obj.GetName()})
}

func clusterRoleKey(obj client.Object) string {
	return missingRoleKey(policyapi.MissingRole{Kind: "ClusterRole", Name: obj.GetName()})
}

// SetupWithManager wires three controllers: PolicyBindings, KubernetesAccountGrants and
// ManagedServiceAccounts, each with whatever it reads mapped back to it.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := r.IndexFields(context.Background(), mgr.GetFieldIndexer()); err != nil {
		return err
	}
	if err := ctrl.NewControllerManagedBy(mgr).
		For(&policyapi.PolicyBinding{}).
		Watches(&policyapi.PolicySet{}, enqueueByIndex[policyapi.PolicyBindingList](r, setsIndex, client.Object.GetName)).
		Watches(&corev1.ServiceAccount{}, enqueueByIndex[policyapi.PolicyBindingList](r, subjectIndex, accountKey)).
		Watches(&rbacv1.RoleBinding{}, r.toOwner(bindingOwner)).
		Watches(&rbacv1.ClusterRoleBinding{}, r.toOwner(bindingOwner)).
		// Metadata only: whether a role exists is all a binding depends on.
		WatchesMetadata(&rbacv1.Role{}, enqueueByIndex[policyapi.PolicyBindingList](r, missingRoleIndex, roleKey)).
		WatchesMetadata(&rbacv1.ClusterRole{}, enqueueByIndex[policyapi.PolicyBindingList](r, missingRoleIndex, clusterRoleKey)).
		Complete(r); err != nil {
		return err
	}
	if err := ctrl.NewControllerManagedBy(mgr).
		For(&policyapi.KubernetesAccountGrant{}).
		Watches(&policyapi.KubernetesRoleSet{}, enqueueByIndex[policyapi.KubernetesAccountGrantList](r, roleSetsIndex, client.Object.GetName)).
		Watches(&corev1.ServiceAccount{}, enqueueByIndex[policyapi.KubernetesAccountGrantList](r, accountIndex, accountKey)).
		Watches(&rbacv1.RoleBinding{}, r.toOwner(grantOwner)).
		Watches(&rbacv1.ClusterRoleBinding{}, r.toOwner(grantOwner)).
		WatchesMetadata(&rbacv1.Role{}, enqueueByIndex[policyapi.KubernetesAccountGrantList](r, missingRoleIndex, roleKey)).
		WatchesMetadata(&rbacv1.ClusterRole{}, enqueueByIndex[policyapi.KubernetesAccountGrantList](r, missingRoleIndex, clusterRoleKey)).
		Complete(reconcile.Func(r.ReconcileGrant)); err != nil {
		return err
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&policyapi.ManagedServiceAccount{}).
		Watches(&corev1.ServiceAccount{}, handler.EnqueueRequestsFromMapFunc(r.toManagedAccount)).
		Complete(reconcile.Func(r.ReconcileManagedServiceAccount))
}
