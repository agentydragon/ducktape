// Package agentconfig holds the kubespand agent YAML configuration and loader.
//
// This wraps the upstream kubespan.ConfigSpec with agent-specific fields
// (discovery endpoint, identity file, etc.) that Talos derives from
// MachineConfig but kubespand loads from a YAML file.
package agentconfig

import (
	"fmt"
	"net/netip"
	"os"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"gopkg.in/yaml.v3"
)

// AgentConfig holds the kubespand YAML configuration.
// Fields that map to kubespan.ConfigSpec use matching names and types.
type AgentConfig struct {
	// Fields mapped to kubespan.ConfigSpec:

	ClusterID                   string           `yaml:"cluster_id"`
	SharedSecret                string           `yaml:"shared_secret"`
	ForceRouting                bool             `yaml:"force_routing"`
	MTU                         uint32           `yaml:"mtu"`
	EndpointFilters             []string         `yaml:"endpoint_filters"`
	HarvestExtraEndpoints       bool             `yaml:"harvest_extra_endpoints"`
	ExtraEndpoints              []netip.AddrPort `yaml:"extra_endpoints"`
	AdvertiseKubernetesNetworks bool             `yaml:"advertise_kubernetes_networks"`
	ExcludeAdvertisedNetworks   []netip.Prefix   `yaml:"exclude_advertised_networks"`

	// Agent-specific fields (not in upstream ConfigSpec):

	// DiscoveryEndpoint is the gRPC endpoint for the Talos discovery service.
	// Default: constants.DefaultDiscoveryServiceEndpoint
	DiscoveryEndpoint string `yaml:"discovery_endpoint"`

	// ListenPort is the UDP port for the WireGuard interface.
	ListenPort int `yaml:"listen_port"`

	// IdentityFile is the path to persist the WireGuard keypair.
	IdentityFile string `yaml:"identity_file"`

	// MachineType is advertised to the discovery service ("worker" or "controlplane").
	MachineType string `yaml:"machine_type"`

	// InsecureDiscovery disables TLS certificate verification for the discovery service.
	InsecureDiscovery bool `yaml:"insecure_discovery"`

	// KubeconfigPath is the path to a kubeconfig file for K8s API access.
	// Required when advertise_kubernetes_networks is true (unless running in-cluster).
	KubeconfigPath string `yaml:"kubeconfig_path"`

	// NodeName is the Kubernetes node name for this machine.
	// Required when advertise_kubernetes_networks is true.
	NodeName string `yaml:"node_name"`

	// ServiceCIDRs are Kubernetes service network ranges to advertise.
	// Static equivalent of PodCIDRs (which come from K8s API dynamically).
	// Only used when advertise_kubernetes_networks is true.
	ServiceCIDRs []netip.Prefix `yaml:"service_cidrs"`
}

// Load reads and validates a YAML config file.
func Load(path string) (*AgentConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config %s: %w", path, err)
	}

	cfg := &AgentConfig{
		DiscoveryEndpoint: constants.DefaultDiscoveryServiceEndpoint,
		ListenPort:        constants.KubeSpanDefaultPort,
		MTU:               constants.KubeSpanLinkMTU,
		IdentityFile:      "/var/lib/kubespan/identity.yaml",
		MachineType:       "worker",
	}

	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parsing config %s: %w", path, err)
	}

	if cfg.ClusterID == "" {
		return nil, fmt.Errorf("cluster_id is required")
	}
	if cfg.SharedSecret == "" {
		return nil, fmt.Errorf("shared_secret is required")
	}
	if cfg.MTU < constants.KubeSpanLinkMinimumMTU {
		return nil, fmt.Errorf("mtu must be at least %d", constants.KubeSpanLinkMinimumMTU)
	}
	if cfg.AdvertiseKubernetesNetworks && cfg.NodeName == "" {
		return nil, fmt.Errorf("node_name is required when advertise_kubernetes_networks is true")
	}

	return cfg, nil
}

// ToConfigSpec converts agent config to upstream kubespan.ConfigSpec for COSI injection.
func (ac *AgentConfig) ToConfigSpec() kubespan.ConfigSpec {
	return kubespan.ConfigSpec{
		Enabled:                     true,
		ClusterID:                   ac.ClusterID,
		SharedSecret:                ac.SharedSecret,
		ForceRouting:                ac.ForceRouting,
		MTU:                         ac.MTU,
		EndpointFilters:             ac.EndpointFilters,
		HarvestExtraEndpoints:       ac.HarvestExtraEndpoints,
		ExtraEndpoints:              ac.ExtraEndpoints,
		AdvertiseKubernetesNetworks: ac.AdvertiseKubernetesNetworks,
		ExcludeAdvertisedNetworks:   ac.ExcludeAdvertisedNetworks,
	}
}
