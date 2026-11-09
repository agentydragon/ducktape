package policycontroller

import (
	"context"
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapplyv1 "github.com/agentydragon/ducktape/agentplane/policy/client/applyconfigurations/api/v1alpha1"
)

// ReconcileManagedServiceAccount keeps the ServiceAccount a ManagedServiceAccount stands for, named
// like it in the accounts namespace, and the status naming that incarnation. It deletes the account
// at its expiresAt, and deletes the ServiceAccounts it created for an account that is gone,
// recognizing them by UID label: the ServiceAccount is in another namespace, so no ownerReference
// reaches it.
func (r *Reconciler) ReconcileManagedServiceAccount(ctx context.Context, req reconcile.Request) (result ctrl.Result, err error) {
	var account policyapi.ManagedServiceAccount
	if err := r.Get(ctx, req.NamespacedName, &account); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, r.pruneServiceAccounts(ctx, req.Name, "")
		}
		return ctrl.Result{}, err
	}
	requeue, expired, err := r.expire(ctx, &account, account.Spec.ExpiresAt)
	if err != nil {
		return ctrl.Result{}, err
	}
	if expired {
		// Revoke in this pass rather than the NotFound reconcile the deletion triggers.
		return ctrl.Result{}, r.pruneServiceAccounts(ctx, account.Name, "")
	}
	defer func() {
		if err == nil {
			result.RequeueAfter = requeue
		}
	}()
	if account.Spec.APIServer != LocalAPIServer {
		// The status has no condition to report this on. Nothing is created, and a local
		// ServiceAccount left from an earlier spec is removed.
		ctrl.LoggerFrom(ctx).Info("ManagedServiceAccount names an apiserver this controller does not reach",
			"apiServer", account.Spec.APIServer)
		return ctrl.Result{}, errors.Join(r.pruneServiceAccounts(ctx, account.Name, ""), r.applyAccountStatus(ctx, &account, nil))
	}
	// A previous incarnation's ServiceAccount at the same name goes first, so this one's can be
	// created in its place.
	if err := r.pruneServiceAccounts(ctx, account.Name, account.UID); err != nil {
		return ctrl.Result{}, err
	}
	created, err := r.ensureServiceAccount(ctx, &account)
	return ctrl.Result{}, errors.Join(err, r.applyAccountStatus(ctx, &account, created))
}

// pruneServiceAccounts deletes the ServiceAccounts created for ManagedServiceAccounts named name,
// except the one carrying keep's UID label, and every native binding granting each of them.
func (r *Reconciler) pruneServiceAccounts(ctx context.Context, name string, keep types.UID) error {
	var accounts corev1.ServiceAccountList
	if err := r.List(ctx, &accounts, client.InNamespace(r.AccountsNamespace),
		client.MatchingLabels{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace},
		client.MatchingFields{managedAccountIdx: name}); err != nil {
		return err
	}
	for i := range accounts.Items {
		account := &accounts.Items[i]
		owner, ok := account.Labels[ManagedAccountUIDLabel]
		if !ok || types.UID(owner) == keep {
			continue
		}
		uid := account.UID
		if err := client.IgnoreNotFound(r.Delete(ctx, account, client.Preconditions{UID: &uid})); err != nil {
			return err
		}
		if err := r.pruneNativesFor(ctx, uid); err != nil {
			return err
		}
	}
	return nil
}

// pruneNativesFor deletes the native bindings of every binding and grant that grant the ServiceAccount
// incarnation uid. Under impersonation these, not the ServiceAccount, are the authority, and RBAC
// matches by name, so they go in the same pass instead of waiting for each grant's own reconcile.
// Listed uncached, so one created a moment ago is not missed; a grant reconcile racing this one sees
// the ServiceAccount gone and prunes anything it recreated.
func (r *Reconciler) pruneNativesFor(ctx context.Context, uid types.UID) error {
	labels := client.MatchingLabels{ManagedByLabel: FieldManager, PolicyNamespaceLbl: r.PolicyNamespace}
	var roleBindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := r.APIReader.List(ctx, &roleBindings, labels); err != nil {
		return err
	}
	if err := r.APIReader.List(ctx, &clusterBindings, labels); err != nil {
		return err
	}
	var natives []client.Object
	for i := range roleBindings.Items {
		natives = append(natives, &roleBindings.Items[i])
	}
	for i := range clusterBindings.Items {
		natives = append(natives, &clusterBindings.Items[i])
	}
	for _, native := range natives {
		if native.GetAnnotations()[SubjectUIDAnn] != string(uid) {
			continue
		}
		if err := r.deleteNative(ctx, native); err != nil {
			return err
		}
	}
	return nil
}

// ensureServiceAccount returns the ServiceAccount created for account, creating it if missing. One
// at its name that was not created for this account is a collision; it is never adopted.
func (r *Reconciler) ensureServiceAccount(ctx context.Context, account *policyapi.ManagedServiceAccount) (*corev1.ServiceAccount, error) {
	key := types.NamespacedName{Namespace: r.AccountsNamespace, Name: account.Name}
	var existing corev1.ServiceAccount
	// Uncached, as for native bindings: the cache may not yet hold one created a moment ago.
	err := r.APIReader.Get(ctx, key, &existing)
	if err == nil {
		labels := existing.Labels
		if labels[ManagedByLabel] != FieldManager || labels[PolicyNamespaceLbl] != r.PolicyNamespace ||
			labels[ManagedAccountUIDLabel] != string(account.UID) {
			return nil, fmt.Errorf("refusing to adopt ServiceAccount %s, which was not created for ManagedServiceAccount %s uid=%s", key, account.Name, account.UID)
		}
		return &existing, nil
	}
	if !apierrors.IsNotFound(err) {
		return nil, err
	}
	automount := false
	created := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: key.Namespace,
			Name:      key.Name,
			Labels: map[string]string{
				ManagedByLabel:         FieldManager,
				PolicyNamespaceLbl:     r.PolicyNamespace,
				ManagedAccountUIDLabel: string(account.UID),
			},
			Annotations: map[string]string{ManagedAccountAnnotation: account.Name},
		},
		// Agentplane acts as the account by impersonation; no Pod runs as it.
		AutomountServiceAccountToken: &automount,
	}
	if err := r.Create(ctx, created); err != nil {
		return nil, fmt.Errorf("creating ServiceAccount %s: %w", key, err)
	}
	return created, nil
}

// applyAccountStatus records the ServiceAccount incarnation created for account, or none.
func (r *Reconciler) applyAccountStatus(ctx context.Context, account *policyapi.ManagedServiceAccount, created *corev1.ServiceAccount) error {
	var want *policyapi.CreatedServiceAccount
	if created != nil {
		want = &policyapi.CreatedServiceAccount{Namespace: created.Namespace, Name: created.Name, UID: created.UID}
	}
	current := account.Status.ServiceAccount
	if (current == nil && want == nil) || (current != nil && want != nil && *current == *want) {
		return nil
	}
	status := policyapplyv1.ManagedServiceAccountStatus()
	if want != nil {
		status.WithServiceAccount(policyapplyv1.CreatedServiceAccount().WithNamespace(want.Namespace).WithName(want.Name).WithUID(want.UID))
	}
	return r.Status().Apply(ctx, policyapplyv1.ManagedServiceAccount(account.Name, account.Namespace).WithStatus(status),
		client.FieldOwner(FieldManager), client.ForceOwnership)
}

// toManagedAccount maps a ServiceAccount the controller created back to its ManagedServiceAccount.
func (r *Reconciler) toManagedAccount(_ context.Context, obj client.Object) []reconcile.Request {
	name, ok := obj.GetAnnotations()[ManagedAccountAnnotation]
	if !ok || obj.GetNamespace() != r.AccountsNamespace {
		return nil
	}
	return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: r.PolicyNamespace, Name: name}}}
}
