package main

import (
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

// Namespace for all kubespand resources.
const KubespanNamespace = resource.Namespace("kubespan")

// Resource type constants.
const (
	ConfigType     = resource.Type("Configs.kubespand")
	IdentityType   = resource.Type("Identities.kubespand")
	PeerSpecType   = resource.Type("PeerSpecs.kubespand")
	PeerStatusType = resource.Type("PeerStatuses.kubespand")
)

// Singleton resource IDs.
const (
	ConfigID   = resource.ID("kubespand")
	IdentityID = resource.ID("kubespand")
)

// --- Config resource ---

// Config is the COSI resource for kubespand configuration.
type Config = typed.Resource[ConfigSpec, ConfigExtension]

// ConfigExtension provides COSI resource metadata for Config.
type ConfigExtension struct{}

// ResourceDefinition implements typed.Extension.
func (ConfigExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             ConfigType,
		DefaultNamespace: KubespanNamespace,
	}
}

// NewConfig creates a new Config resource.
func NewConfig(ns resource.Namespace, id resource.ID) *Config {
	return typed.NewResource[ConfigSpec, ConfigExtension](
		resource.NewMetadata(ns, ConfigType, id, resource.VersionUndefined),
		ConfigSpec{},
	)
}

// --- Identity resource ---

// Identity is the COSI resource for the node's KubeSpan identity.
type Identity = typed.Resource[IdentitySpec, IdentityExtension]

// IdentityExtension provides COSI resource metadata for Identity.
type IdentityExtension struct{}

// ResourceDefinition implements typed.Extension.
func (IdentityExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             IdentityType,
		DefaultNamespace: KubespanNamespace,
	}
}

// NewIdentity creates a new Identity resource.
func NewIdentity(ns resource.Namespace, id resource.ID) *Identity {
	return typed.NewResource[IdentitySpec, IdentityExtension](
		resource.NewMetadata(ns, IdentityType, id, resource.VersionUndefined),
		IdentitySpec{},
	)
}

// --- PeerSpec resource ---

// PeerSpec is the COSI resource for a discovered KubeSpan peer.
type PeerSpec = typed.Resource[PeerSpecSpec, PeerSpecExtension]

// PeerSpecExtension provides COSI resource metadata for PeerSpec.
type PeerSpecExtension struct{}

// ResourceDefinition implements typed.Extension.
func (PeerSpecExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             PeerSpecType,
		DefaultNamespace: KubespanNamespace,
	}
}

// NewPeerSpec creates a new PeerSpec resource.
func NewPeerSpec(ns resource.Namespace, id resource.ID) *PeerSpec {
	return typed.NewResource[PeerSpecSpec, PeerSpecExtension](
		resource.NewMetadata(ns, PeerSpecType, id, resource.VersionUndefined),
		PeerSpecSpec{},
	)
}

// --- PeerStatus resource ---

// PeerStatus is the COSI resource for a peer's live WireGuard state.
type PeerStatus = typed.Resource[PeerStatusSpec, PeerStatusExtension]

// PeerStatusExtension provides COSI resource metadata for PeerStatus.
type PeerStatusExtension struct{}

// ResourceDefinition implements typed.Extension.
func (PeerStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             PeerStatusType,
		DefaultNamespace: KubespanNamespace,
	}
}

// NewPeerStatus creates a new PeerStatus resource.
func NewPeerStatus(ns resource.Namespace, id resource.ID) *PeerStatus {
	return typed.NewResource[PeerStatusSpec, PeerStatusExtension](
		resource.NewMetadata(ns, PeerStatusType, id, resource.VersionUndefined),
		PeerStatusSpec{},
	)
}
