package sandboxcontroller

import (
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/selection"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
)

// CacheByObject restricts the manager's cache. Of the kinds the controller creates or watches by the
// Sandbox label (Pods, PVCs, ServiceAccounts, ConfigMaps, Services, PolicyBindings and, with
// KubeVirt, VMs and VMIs) it holds only objects carrying the label, so an unrelated object in the
// namespace is neither stored nor enqueued. Events carry no labels: it holds those about any Pod. Of
// Leases it holds the Runner Gateway's connection Leases, not the leader election Lease. It holds
// every WorkloadCredential of the namespace, which carry no label.
//
// Every object of those kinds that the controller must see carries the label: the controller applies
// it to what it creates.
func CacheByObject(kubeVirt bool) (map[client.Object]cache.ByObject, error) {
	labelled, err := labels.NewRequirement(sandboxapi.SandboxLabel, selection.Exists, nil)
	if err != nil {
		return nil, err
	}
	ours := labels.NewSelector().Add(*labelled)
	byObject := map[client.Object]cache.ByObject{
		&corev1.Pod{}:                   {Label: ours},
		&corev1.PersistentVolumeClaim{}: {Label: ours},
		&corev1.ServiceAccount{}:        {Label: ours},
		&corev1.ConfigMap{}:             {Label: ours},
		&corev1.Service{}:               {Label: ours},
		&policyapi.PolicyBinding{}:      {Label: ours},
		&corev1.Event{}:                 {Field: fields.OneTermEqualSelector("involvedObject.kind", "Pod")},
	}
	if kubeVirt {
		byObject[&kubevirtv1.VirtualMachine{}] = cache.ByObject{Label: ours}
		byObject[&kubevirtv1.VirtualMachineInstance{}] = cache.ByObject{Label: ours}
	}
	connection, err := labels.NewRequirement(sandboxlease.EnvironmentRuntimeConnectionLabel, selection.Equals, []string{"true"})
	if err != nil {
		return nil, err
	}
	byObject[&coordinationv1.Lease{}] = cache.ByObject{Label: labels.NewSelector().Add(*connection)}
	return byObject, nil
}
