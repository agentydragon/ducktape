package policycontroller

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapply "github.com/agentydragon/ducktape/agentplane/policy/client/apply"
)

// RoleSetUnavailable is the reason a grant's condition gives when it names a KubernetesRoleSet that
// does not exist.
const RoleSetUnavailable = "RoleSetUnavailable"

// ReconcileGrant converges one KubernetesAccountGrant exactly as Reconcile does a PolicyBinding: its
// native bindings, its KubernetesReady condition and missingRoles, and its expiry.
func (r *Reconciler) ReconcileGrant(ctx context.Context, req reconcile.Request) (result ctrl.Result, err error) {
	var grant policyapi.KubernetesAccountGrant
	if err := r.Get(ctx, req.NamespacedName, &grant); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, r.converge(ctx, grantOwner, req.Name, nil)
		}
		return ctrl.Result{}, err
	}
	var e expansion
	if conflictingExpiry(&grant, grant.Spec.ExpiresAt) {
		e = conflictingExpiryExpansion()
	} else {
		var requeue time.Duration
		var expired bool
		// Assigns the named err, which the deferred requeue reads.
		if requeue, expired, err = r.expire(ctx, &grant, grant.Spec.ExpiresAt); err != nil {
			return ctrl.Result{}, err
		} else if expired {
			// Not live: revoke in this pass rather than the NotFound reconcile the deletion triggers.
			return ctrl.Result{}, r.converge(ctx, grantOwner, grant.Name, nil)
		}
		defer func() {
			if err == nil {
				result.RequeueAfter = requeue
			}
		}()
		if e, err = r.expandGrant(ctx, &grant); err != nil {
			return ctrl.Result{}, err
		}
	}
	if err := r.converge(ctx, grantOwner, grant.Name, e.desired); err != nil {
		e.reason, e.message = "ApplyFailed", err.Error()
		return ctrl.Result{}, errors.Join(err, r.applyGrantStatus(ctx, &grant, e))
	}
	return ctrl.Result{}, r.applyGrantStatus(ctx, &grant, e)
}

func (r *Reconciler) expandGrant(ctx context.Context, grant *policyapi.KubernetesAccountGrant) (expansion, error) {
	spec := grant.Spec
	if spec.Account.APIServer != LocalAPIServer {
		return expansion{reason: "APIServerNotConfigured", message: fmt.Sprintf("apiServer %s is not one this controller reaches; it reaches only %s", spec.Account.APIServer, LocalAPIServer)}, nil
	}
	account, e, err := r.subject(ctx, spec.Account.Namespace, spec.Account.Name, r.grantNamespaces())
	if account == nil {
		return e, err
	}
	var natives []native
	missing := unavailableSets{reason: RoleSetUnavailable, kind: "KubernetesRoleSets"}
	for _, setName := range spec.RoleSets {
		var set policyapi.KubernetesRoleSet
		if err := r.Get(ctx, types.NamespacedName{Namespace: grant.Namespace, Name: setName}, &set); err != nil {
			if !apierrors.IsNotFound(err) {
				return expansion{}, err
			}
			// The grant's other sets still grant.
			missing.names = append(missing.names, setName)
			continue
		}
		for _, rule := range set.Spec.Rules {
			scoped, err := roleSetScopes(rule)
			if err != nil {
				return expansion{}, fmt.Errorf("KubernetesRoleSet %s: %w", setName, err)
			}
			natives = append(natives, scoped...)
		}
	}
	return r.expandNatives(ctx, grantOwner, grant, account, natives, missing)
}

// grantNamespaces are the namespaces whose ServiceAccounts a grant may name.
func (r *Reconciler) grantNamespaces() []string {
	if slices.Contains(r.SubjectNamespaces, r.AccountsNamespace) {
		return r.SubjectNamespaces
	}
	return append(slices.Clone(r.SubjectNamespaces), r.AccountsNamespace)
}

func roleSetScopes(rule policyapi.KubernetesRule) ([]native, error) {
	switch rule.Type {
	case "role_binding":
		var out []native
		for _, ns := range rule.Namespaces {
			out = append(out, native{Namespace: ns, Role: *rule.Role})
		}
		return out, nil
	case "cluster_role_binding":
		return []native{{Role: policyapi.RoleRef{Kind: "ClusterRole", Name: rule.ClusterRole}}}, nil
	}
	// The CRD admits only the two types above.
	return nil, fmt.Errorf("unknown rule type %q", rule.Type)
}

// applyGrantStatus is applyStatus for a grant.
func (r *Reconciler) applyGrantStatus(ctx context.Context, grant *policyapi.KubernetesAccountGrant, e expansion) error {
	configuration, changed := policyapply.GrantConditionApply(grant, e.condition())
	if !changed && slices.Equal(grant.Status.MissingRoles, e.missingRoles) {
		return nil
	}
	configuration.Status.WithMissingRoles(e.missingRoleConfigurations()...)
	return r.Status().Apply(ctx, configuration, client.FieldOwner(FieldManager), client.ForceOwnership)
}
