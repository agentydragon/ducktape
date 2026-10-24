// Package policyaccess lists policy sets and owns the app's runtime PolicyBinding operations.
package policyaccess

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	appsandboxes "github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
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

// ActionPolicySets returns the Action Service's inventory view: every set, ordered by name, with
// the same refusal boundary it uses when parsing action_auto_approve rules.
func (a *Access) ActionPolicySets(ctx context.Context) ([]ActionPolicySetView, error) {
	var sets policyapi.PolicySetList
	if err := a.objects.List(ctx, &sets, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list PolicySets: %w", err)
	}
	views := make([]ActionPolicySetView, 0, len(sets.Items))
	for index := range sets.Items {
		set := &sets.Items[index]
		view := ActionPolicySetView{Name: set.Name, Generation: set.Generation}
		for ruleIndex, rule := range set.Spec.Rules {
			if rule.Type != policies.RuleType {
				continue
			}
			if _, err := policies.ParseRule(rule); err != nil {
				reason := truncateReason(fmt.Sprintf("rules[%d]: %v", ruleIndex, err))
				view.Refused = &reason
				break
			}
		}
		views = append(views, view)
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Name < views[j].Name })
	return views, nil
}

// BindingsFor returns the subject's PolicyBindings by name, projecting every named set and each
// policy reader's status without reading credential values.
func (a *Access) BindingsFor(ctx context.Context, subject *sandboxespb.ServiceAccount) ([]BindingView, error) {
	if subject == nil || subject.GetNamespace() == "" || subject.GetName() == "" {
		return nil, errors.New("ServiceAccount namespace and name are required")
	}
	bindings, sets, credentials, err := a.listPolicySnapshot(ctx)
	if err != nil {
		return nil, err
	}
	setViews, err := credentialProjection(sets.Items, credentials.Items)
	if err != nil {
		return nil, err
	}
	return bindingViews(bindings.Items, setViews, policyapi.ServiceAccountRef{
		Namespace: subject.GetNamespace(), Name: subject.GetName(),
	}), nil
}

// BindingsFromSnapshot projects a subject's bindings from a caller-owned, deep-copied live
// Kubernetes snapshot. It shares the same sanitization and refusal rules as BindingsFor without
// performing a second set of API-server reads that could disagree with the watched frame.
func BindingsFromSnapshot(
	bindings []policyapi.PolicyBinding,
	sets []policyapi.PolicySet,
	credentials []egressapi.EgressCredential,
	subject *sandboxespb.ServiceAccount,
) ([]BindingView, error) {
	if subject == nil || subject.GetNamespace() == "" || subject.GetName() == "" {
		return nil, errors.New("ServiceAccount namespace and name are required")
	}
	setViews, err := credentialProjection(sets, credentials)
	if err != nil {
		return nil, err
	}
	return bindingViews(bindings, setViews, policyapi.ServiceAccountRef{
		Namespace: subject.GetNamespace(), Name: subject.GetName(),
	}), nil
}

func (a *Access) listPolicySnapshot(
	ctx context.Context,
) (policyapi.PolicyBindingList, policyapi.PolicySetList, egressapi.EgressCredentialList, error) {
	var bindings policyapi.PolicyBindingList
	var sets policyapi.PolicySetList
	var credentials egressapi.EgressCredentialList
	var errs [3]error
	var group sync.WaitGroup
	group.Add(len(errs))
	go func() {
		defer group.Done()
		errs[0] = a.objects.List(ctx, &bindings, client.InNamespace(a.namespace))
	}()
	go func() {
		defer group.Done()
		errs[1] = a.objects.List(ctx, &sets, client.InNamespace(a.namespace))
	}()
	go func() {
		defer group.Done()
		errs[2] = a.objects.List(ctx, &credentials, client.InNamespace(a.namespace))
	}()
	group.Wait()
	if errs[0] != nil {
		return bindings, sets, credentials, fmt.Errorf("list PolicyBindings: %w", errs[0])
	}
	if errs[1] != nil {
		return bindings, sets, credentials, fmt.Errorf("list PolicySets: %w", errs[1])
	}
	if errs[2] != nil {
		return bindings, sets, credentials, fmt.Errorf("list EgressCredentials: %w", errs[2])
	}
	return bindings, sets, credentials, nil
}

// Grant creates a new generated-name binding owned by the Sandbox. Repeated grants create
// independent bindings so each expiry and later revocation belongs to its own grant.
func (a *Access) Grant(
	ctx context.Context,
	sandbox *sandboxespb.Sandbox,
	policySets []string,
	expiresAt *metav1.Time,
) (*BindingView, error) {
	if sandbox == nil || sandbox.GetName() == "" || sandbox.GetUid() == "" ||
		sandbox.GetServiceAccount() == nil || sandbox.GetServiceAccount().GetNamespace() == "" ||
		sandbox.GetServiceAccount().GetName() == "" {
		return nil, errors.New("Sandbox name, UID, and ServiceAccount are required")
	}
	if err := a.requireKnownPolicySets(ctx, policySets); err != nil {
		return nil, err
	}
	controller := false
	blockOwnerDeletion := false
	binding := &policyapi.PolicyBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: policyapi.SchemeGroupVersion.String(), Kind: "PolicyBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Namespace: a.namespace, GenerateName: sandbox.GetName() + "-",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: sandbox.GetName(), UID: types.UID(sandbox.GetUid()),
				Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.ServiceAccountRef{
				Namespace: sandbox.GetServiceAccount().GetNamespace(), Name: sandbox.GetServiceAccount().GetName(),
			},
			PolicySets: append([]string(nil), policySets...), ExpiresAt: expiresAt,
		},
	}
	if err := a.objects.Create(ctx, binding); err != nil {
		return nil, err
	}
	views, err := a.BindingsFor(ctx, sandbox.GetServiceAccount())
	if err != nil {
		return nil, err
	}
	for index := range views {
		if views[index].Name == binding.Name {
			return &views[index], nil
		}
	}
	return nil, &CreatedBindingNotVisibleError{Name: binding.Name}
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
	return &appsandboxes.UnknownPolicySetError{Names: missing, Kind: "PolicySet"}
}
