// Package policyaccess lists policy sets and owns the app's runtime PolicyBinding operations.
package policyaccess

import (
	"context"
	"errors"
	"fmt"
	"sort"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Access lists policy sets and manages app-created PolicyBindings in one namespace.
type Access struct {
	namespace string
	objects   client.Client
}

// New creates access to the policy objects in namespace.
func New(namespace string, objects client.Client) *Access {
	return &Access{namespace: namespace, objects: objects}
}

// PolicySetNames returns every PolicySet name in the access namespace, regardless of rule type.
func (a *Access) PolicySetNames(ctx context.Context) ([]string, error) {
	var sets policyapi.PolicySetList
	if err := a.objects.List(ctx, &sets, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list PolicySets: %w", err)
	}
	names := make([]string, 0, len(sets.Items))
	for _, set := range sets.Items {
		names = append(names, set.Name)
	}
	sort.Strings(names)
	return names, nil
}

// Grant creates a new generated-name binding owned by the Sandbox. Repeated grants create
// independent bindings so each expiry and later revocation belongs to its own grant.
func (a *Access) Grant(
	ctx context.Context,
	sandbox *environmentpb.EnvironmentDescriptor,
	policySets []string,
	expiresAt *metav1.Time,
) error {
	var target *identitypb.EnvironmentTarget
	var account *identitypb.ServiceAccountIdentity
	if sandbox != nil {
		target, account = sandbox.GetTarget(), sandbox.GetExecutionAccount()
	}
	if target == nil || target.GetNamespace() != a.namespace || target.GetName() == "" || target.GetUid() == "" ||
		account == nil || account.GetAccount() == nil || account.GetAccount().GetNamespace() == "" || account.GetAccount().GetName() == "" {
		return errors.New("Sandbox name, UID, and ServiceAccount are required")
	}
	if err := a.requireKnownPolicySets(ctx, policySets); err != nil {
		return err
	}
	controller := false
	blockOwnerDeletion := false
	binding := &policyapi.PolicyBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: a.namespace, GenerateName: target.GetName() + "-",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: target.GetName(), UID: types.UID(target.GetUid()),
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.ServiceAccountRef{
				Namespace: account.GetAccount().GetNamespace(), Name: account.GetAccount().GetName(),
			},
			PolicySets: append([]string(nil), policySets...), ExpiresAt: expiresAt,
		},
	}
	return a.objects.Create(ctx, binding)
}

// Revoke deletes only an operator-owned binding. Git-owned resources are restored by Flux, and
// Sandbox-owned resources are restored by the Sandbox controller.
func (a *Access) Revoke(ctx context.Context, name string) error {
	binding := &policyapi.PolicyBinding{}
	if err := a.objects.Get(ctx, client.ObjectKey{Namespace: a.namespace, Name: name}, binding); err != nil {
		if apierrors.IsNotFound(err) {
			return &BindingNotFoundError{Name: name}
		}
		return fmt.Errorf("read PolicyBinding %q: %w", name, err)
	}
	switch bindingProvenance(binding) {
	case BindingFromGit:
		return &FluxOwnedBindingError{Name: name}
	case BindingFromSandbox:
		return &SandboxOwnedBindingError{Name: name}
	case BindingFromOperator:
		return a.objects.Delete(ctx, binding)
	default:
		panic("unreachable PolicyBinding provenance")
	}
}

func (a *Access) requireKnownPolicySets(ctx context.Context, names []string) error {
	existing, err := a.PolicySetNames(ctx)
	if err != nil {
		return err
	}
	known := make(map[string]struct{}, len(existing))
	for _, name := range existing {
		known[name] = struct{}{}
	}
	missingSet := make(map[string]struct{})
	for _, name := range names {
		if _, ok := known[name]; !ok {
			missingSet[name] = struct{}{}
		}
	}
	if len(missingSet) == 0 {
		return nil
	}
	missing := make([]string, 0, len(missingSet))
	for name := range missingSet {
		missing = append(missing, name)
	}
	sort.Strings(missing)
	return &policy.UnknownPolicySetError{Names: missing, Kind: "PolicySet"}
}
