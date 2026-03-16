// Package agentconfig defines the kubespand-specific COSI resource type for
// configuration fields that have no upstream Talos equivalent.
//
// Fields that map to Talos resource types are delivered via those types instead:
//   - kubespan.Config — WireGuard/routing settings (upstream)
//   - cluster.Config  — discovery service settings (upstream)
//
// This resource holds the remainder: paths, ports, feature flags, and K8s
// integration settings unique to non-Talos Linux hosts.
package agentconfig

import (
	"net/netip"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

const (
	// ResourceType is the COSI resource type for the kubespand agent config.
	ResourceType = resource.Type("AgentConfig.kubespand")

	// ResourceID is the singleton resource ID.
	ResourceID = resource.ID("agent-config")

	// ResourceNamespace is the COSI namespace for this resource.
	ResourceNamespace = "kubespan"
)

// Spec holds kubespand-specific configuration fields with no Talos resource equivalent.
type Spec struct {
	// IdentityFile is the path to persist the WireGuard keypair.
	// Talos uses the STATE partition; kubespand uses a flat file.
	IdentityFile string

	// ListenPort is the WireGuard UDP port.
	// Talos hardcodes constants.KubeSpanDefaultPort.
	ListenPort int

	// MachineType is advertised to the discovery service ("worker" or "controlplane").
	// Talos reads from MachineConfig.
	MachineType string

	// KubeconfigPath is the path to a kubeconfig file for K8s API access.
	// Talos has direct API access without kubeconfig.
	KubeconfigPath string

	// NodeName is the Kubernetes node name for this machine.
	// Talos derives this internally.
	NodeName string

	// ServiceCIDRs are Kubernetes service network ranges to advertise.
	ServiceCIDRs []netip.Prefix

	// ClusterEndpoint is the cluster API server URL (e.g., "https://host:6443").
	// Used by KubePrism as the primary upstream before CP peers are discovered.
	ClusterEndpoint string

	// KubePrismEnabled turns on the KubePrism local load balancer.
	KubePrismEnabled bool

	// KubePrismHost is the local address to bind for KubePrism.
	KubePrismHost string

	// KubePrismPort is the local port to listen on for KubePrism.
	KubePrismPort int

	// CACrt is the PEM-encoded Talos CA certificate for the trustd CSR flow.
	CACrt string

	// Token is the Talos machine token for trustd authentication.
	Token string

	// CertSANs are additional IPs or DNS names for the apid TLS certificate.
	// Matches Talos machine.certSANs.
	CertSANs []string
}

// DeepCopy implements typed.DeepCopyable.
func (s Spec) DeepCopy() Spec {
	cp := s
	if s.ServiceCIDRs != nil {
		cp.ServiceCIDRs = make([]netip.Prefix, len(s.ServiceCIDRs))
		copy(cp.ServiceCIDRs, s.ServiceCIDRs)
	}
	if s.CertSANs != nil {
		cp.CertSANs = make([]string, len(s.CertSANs))
		copy(cp.CertSANs, s.CertSANs)
	}
	return cp
}

// Resource is the COSI typed resource for kubespand agent config.
type Resource = typed.Resource[Spec, Extension]

// Extension provides resource definition metadata.
type Extension struct{}

// ResourceDefinition implements typed.Extension.
func (Extension) ResourceDefinition() meta.ResourceDefinitionSpec {
	return meta.ResourceDefinitionSpec{
		Type:             ResourceType,
		DefaultNamespace: ResourceNamespace,
	}
}

// NewResource creates a new agent config COSI resource.
func NewResource() *Resource {
	return typed.NewResource[Spec, Extension](
		resource.NewMetadata(ResourceNamespace, ResourceType, ResourceID, resource.VersionUndefined),
		Spec{},
	)
}

// SpecFromAgentConfig converts the YAML-loaded AgentConfig into the COSI resource spec,
// extracting only the kubespand-specific fields.
func SpecFromAgentConfig(ac *AgentConfig) Spec {
	return Spec{
		IdentityFile:     ac.Kubespan.IdentityFile,
		ListenPort:       ac.Kubespan.ListenPort,
		MachineType:      ac.Discovery.MachineType,
		KubeconfigPath:   ac.Kubernetes.KubeconfigPath,
		NodeName:         ac.Kubernetes.NodeName,
		ServiceCIDRs:     ac.Kubernetes.ServiceCIDRs,
		ClusterEndpoint:  ac.Cluster.Endpoint,
		KubePrismEnabled: ac.KubePrism.Enabled,
		KubePrismHost:    ac.KubePrism.Host,
		KubePrismPort:    ac.KubePrism.Port,
		CACrt:            ac.Api.CACrt,
		Token:            ac.Api.Token,
		CertSANs:         ac.Api.CertSANs,
	}
}
