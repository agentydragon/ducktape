package policycontroller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	egressapplyv1 "github.com/agentydragon/ducktape/agentplane/egress/client/applyconfigurations/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapplyv1 "github.com/agentydragon/ducktape/agentplane/policy/client/applyconfigurations/api/v1alpha1"
)

const (
	// The local API server's in-cluster name, where the proxy impersonates. Must equal KUBERNETES_HOST
	// in cluster/cdk8s/agentplane/egress.py, which renders static accounts' use rules and pins this
	// host in the admission policy over what the controller writes.
	localAPIServerHost = "kubernetes.default.svc.cluster.local"
	// Must equal CREDENTIAL_PREFIX in cluster/cdk8s/agentplane/kubernetes_account_ref.py.
	accountCredentialPrefix = "k8s-account-"

	EgressCredentialConflict = "EgressCredentialConflict"
	PolicySetConflict        = "PolicySetConflict"
)

// AccountCredentialName names the EgressCredential of the account `name` and the one-rule PolicySet
// a binding names to act as it.
func AccountCredentialName(name string) string { return accountCredentialPrefix + name }

// errForeignAccountUse marks an EgressCredential or PolicySet at a managed account's use name that
// the account does not control, or that is being deleted.
type errForeignAccountUse struct{ reason, message string }

func (e *errForeignAccountUse) Error() string { return e.message }

// ensureAccountUse keeps what makes the account usable, as cdk8s renders it for a static account
// (cluster/cdk8s/agentplane/kubernetes_accounts.py): the EgressCredential the proxy impersonates
// serviceAccount with, and the one-rule PolicySet a binding names to present it. The account
// controls both, so garbage collection removes them with it.
func (r *Reconciler) ensureAccountUse(ctx context.Context, account *policyapi.ManagedServiceAccount, serviceAccount *corev1.ServiceAccount) error {
	name := AccountCredentialName(account.Name)
	host := "https://" + localAPIServerHost
	credential := egressapplyv1.EgressCredential(name, r.PolicyNamespace).
		WithSpec(egressapplyv1.EgressCredentialSpec().
			WithDescription(fmt.Sprintf(
				"Kubernetes API server: %s, acting as the %s/%s ServiceAccount: %s Use kubectl --server %s --token %s "+
					"for Kubernetes commands. Check agentplane egress rules for the live host and placeholder.",
				host, serviceAccount.Namespace, serviceAccount.Name, accountDescription(account), host, resources.Placeholder(name),
			)).
			WithSource(egressapplyv1.CredentialSource().WithKubernetesServiceAccount(
				egressapplyv1.KubernetesServiceAccountSource().WithNamespace(serviceAccount.Namespace).WithName(serviceAccount.Name),
			)).
			WithTargets(egressapplyv1.Target().WithHeader("Authorization").WithMethod(egressapi.SchemeToken).WithScheme("Bearer")))
	if err := r.applyOwned(ctx, account, credential, EgressCredentialConflict); err != nil {
		return err
	}
	// No method or path list, as for a static account: what the account may do is the API server's
	// answer, and narrowing verbs here would be a weaker copy of RBAC.
	set := policyapplyv1.PolicySet(name, r.PolicyNamespace).
		WithSpec(policyapplyv1.PolicySetSpec().WithRules(policyapplyv1.Rule().
			WithType(resources.EgressRuleType).
			WithHosts(localAPIServerHost).
			WithClusterInternal(true).
			WithCredentialRef(policyapplyv1.CredentialRef().WithName(name))))
	return r.applyOwned(ctx, account, set, PolicySetConflict)
}

// accountDescription is what a static account's description says: what the account is.
func accountDescription(account *policyapi.ManagedServiceAccount) string {
	if account.Spec.ExpiresAt == nil {
		return fmt.Sprintf("An account Agentplane created for ManagedServiceAccount %s.", account.Name)
	}
	return fmt.Sprintf("A temporary account Agentplane created for ManagedServiceAccount %s, deleted at %s.",
		account.Name, account.Spec.ExpiresAt.UTC().Format(time.RFC3339))
}

// applyOwned creates intent, an apply configuration, controlled by account, or applies it over the
// object at its name that account controls. One the account does not control, or that is being
// deleted, is a collision and is never adopted: it returns errForeignAccountUse with reason.
func (r *Reconciler) applyOwned(ctx context.Context, account *policyapi.ManagedServiceAccount, intent runtime.ApplyConfiguration, reason string) error {
	fields, err := runtime.DefaultUnstructuredConverter.ToUnstructured(intent)
	if err != nil {
		return err
	}
	desired := &unstructured.Unstructured{Object: fields}
	// Without blockOwnerDeletion, which would need update on the account's finalizers.
	if err := controllerutil.SetControllerReference(account, desired, r.Scheme(), controllerutil.WithBlockOwnerDeletion(false)); err != nil {
		return err
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	// Uncached, as for the ServiceAccount: the cache may not yet hold one created a moment ago.
	err = r.APIReader.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		// A concurrent create fails here and is checked on the retry.
		return r.Create(ctx, desired, client.FieldOwner(FieldManager))
	}
	if err != nil {
		return err
	}
	if !metav1.IsControlledBy(current, account) || !current.GetDeletionTimestamp().IsZero() {
		return &errForeignAccountUse{
			reason: reason,
			message: fmt.Sprintf("refusing to adopt %s %s for ManagedServiceAccount %s uid=%s: it is not controlled by this account, or is being deleted",
				current.GetKind(), current.GetName(), account.Name, account.UID),
		}
	}
	// Both guards bind the ownership decision to the exact observed object version.
	desired.SetUID(current.GetUID())
	desired.SetResourceVersion(current.GetResourceVersion())
	return r.Apply(ctx, client.ApplyConfigurationFromUnstructured(desired), client.FieldOwner(FieldManager), client.ForceOwnership)
}
