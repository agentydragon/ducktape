// Package resources defines COSI resource types for kubespand.
package resources

import (
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/config"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/discovery"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/identity"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
)

// Namespace for all kubespand resources.
const Namespace = resource.Namespace("kubespan")

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
type Config = typed.Resource[config.Spec, ConfigExtension]

// ConfigExtension provides COSI resource metadata for Config.
type ConfigExtension struct{}

// ResourceDefinition implements typed.Extension.
func (ConfigExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             ConfigType,
		DefaultNamespace: Namespace,
	}
}

// NewConfig creates a new Config resource.
func NewConfig(ns resource.Namespace, id resource.ID) *Config {
	return typed.NewResource[config.Spec, ConfigExtension](
		resource.NewMetadata(ns, ConfigType, id, resource.VersionUndefined),
		config.Spec{},
	)
}

// --- Identity resource ---

// Identity is the COSI resource for the node's KubeSpan identity.
type Identity = typed.Resource[identity.Spec, IdentityExtension]

// IdentityExtension provides COSI resource metadata for Identity.
type IdentityExtension struct{}

// ResourceDefinition implements typed.Extension.
func (IdentityExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             IdentityType,
		DefaultNamespace: Namespace,
	}
}

// NewIdentity creates a new Identity resource.
func NewIdentity(ns resource.Namespace, id resource.ID) *Identity {
	return typed.NewResource[identity.Spec, IdentityExtension](
		resource.NewMetadata(ns, IdentityType, id, resource.VersionUndefined),
		identity.Spec{},
	)
}

// --- PeerSpec resource ---

// PeerSpec is the COSI resource for a discovered KubeSpan peer.
type PeerSpec = typed.Resource[discovery.PeerSpec, PeerSpecExtension]

// PeerSpecExtension provides COSI resource metadata for PeerSpec.
type PeerSpecExtension struct{}

// ResourceDefinition implements typed.Extension.
func (PeerSpecExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             PeerSpecType,
		DefaultNamespace: Namespace,
	}
}

// NewPeerSpec creates a new PeerSpec resource.
func NewPeerSpec(ns resource.Namespace, id resource.ID) *PeerSpec {
	return typed.NewResource[discovery.PeerSpec, PeerSpecExtension](
		resource.NewMetadata(ns, PeerSpecType, id, resource.VersionUndefined),
		discovery.PeerSpec{},
	)
}

// --- PeerStatus resource ---

// PeerStatus is the COSI resource for a peer's live WireGuard state.
type PeerStatus = typed.Resource[peerstate.Spec, PeerStatusExtension]

// PeerStatusExtension provides COSI resource metadata for PeerStatus.
type PeerStatusExtension struct{}

// ResourceDefinition implements typed.Extension.
func (PeerStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{
		Type:             PeerStatusType,
		DefaultNamespace: Namespace,
	}
}

// NewPeerStatus creates a new PeerStatus resource.
func NewPeerStatus(ns resource.Namespace, id resource.ID) *PeerStatus {
	return typed.NewResource[peerstate.Spec, PeerStatusExtension](
		resource.NewMetadata(ns, PeerStatusType, id, resource.VersionUndefined),
		peerstate.Spec{},
	)
}
