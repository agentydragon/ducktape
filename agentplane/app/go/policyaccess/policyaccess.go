// Package policyaccess lists PolicySets for app views and forms.
package policyaccess

import (
	"context"
	"fmt"
	"sort"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Access lists PolicySets in one namespace.
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
