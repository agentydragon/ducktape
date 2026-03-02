// Package k8snet defines the KubernetesNetworks COSI resource type,
// which holds the local node's Kubernetes network prefixes (PodCIDRs + ServiceCIDRs)
// for advertisement via the discovery service.
package k8snet

import (
	"net/netip"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

const (
	// Type is the COSI resource type for KubernetesNetworks.
	Type = resource.Type("KubernetesNetworks.kubespan.kubespand")

	// ID is the singleton resource ID (one node = one set of networks).
	ID = resource.ID("local-kubernetes-networks")

	// NamespaceName is the COSI namespace for this resource.
	NamespaceName = "kubespan"
)

// Spec holds the Kubernetes network prefixes (PodCIDRs + ServiceCIDRs) for the local node.
type Spec struct {
	Prefixes []netip.Prefix
}

// DeepCopy implements typed.DeepCopyable.
func (s Spec) DeepCopy() Spec {
	if s.Prefixes == nil {
		return s
	}
	cp := Spec{Prefixes: make([]netip.Prefix, len(s.Prefixes))}
	copy(cp.Prefixes, s.Prefixes)
	return cp
}

// KubernetesNetworks is the COSI typed resource for local Kubernetes networks.
type KubernetesNetworks = typed.Resource[Spec, Extension]

// Extension provides resource definition metadata for KubernetesNetworks.
type Extension struct{}

// ResourceDefinition implements typed.Extension.
func (Extension) ResourceDefinition() meta.ResourceDefinitionSpec {
	return meta.ResourceDefinitionSpec{
		Type:             Type,
		DefaultNamespace: NamespaceName,
	}
}

// New creates a new KubernetesNetworks resource.
func New() *KubernetesNetworks {
	return typed.NewResource[Spec, Extension](
		resource.NewMetadata(NamespaceName, Type, ID, resource.VersionUndefined),
		Spec{},
	)
}
