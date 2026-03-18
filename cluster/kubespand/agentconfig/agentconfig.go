// Package agentconfig holds the kubespand agent YAML configuration and loader.
//
// This wraps the upstream kubespan.ConfigSpec with agent-specific fields
// (discovery endpoint, identity file, etc.) that Talos derives from
// MachineConfig but kubespand loads from a YAML file.
//
// The config is structured into logical groups aligned with Talos conventions:
//   - cluster: identity (matches Talos .spec.cluster.{id,secret})
//   - kubespan: WireGuard interface settings
//   - discovery: discovery service settings
//   - kubernetes: K8s integration (kubespand extension)
package agentconfig

import (
	"crypto/aes"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"os"

	"github.com/siderolabs/talos/pkg/machinery/config/types/v1alpha1"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"gopkg.in/yaml.v3"
)

// AgentConfig holds the kubespand YAML configuration.
type AgentConfig struct {
	Cluster    ClusterConfig    `yaml:"cluster"`
	Kubespan   KubespanConfig   `yaml:"kubespan"`
	Discovery  DiscoveryConfig  `yaml:"discovery"`
	Kubernetes KubernetesConfig `yaml:"kubernetes"`
	KubePrism  KubePrismConfig  `yaml:"kubeprism"`
	Api        ApiConfig        `yaml:"api"`
	Network    NetworkConfig    `yaml:"network"`
}

// NetworkConfig holds static network configuration for the host interface.
// Routes use Talos's v1alpha1.Route type directly, fed through the standard
// Talos route pipeline (RouteConfigController → RouteMergeController → RouteSpecController).
type NetworkConfig struct {
	// Interface is the network interface for static routes. Required when routes are configured.
	Interface string `yaml:"interface,omitempty"`
	// Routes are static routes to apply on the host interface.
	// Uses the same type as Talos machine config routes.
	Routes []*v1alpha1.Route `yaml:"routes,omitempty"`
}

// ApiConfig holds TLS certificate settings for the Talos API integration.
type ApiConfig struct {
	// CACrt is the PEM-encoded Talos CA certificate (from machine.ca.crt).
	// When set with Token, enables the Talos trustd CSR flow to obtain
	// API certificates for apid mTLS on port 50000.
	CACrt string `yaml:"ca_crt"`
	// Token is the Talos machine token for trustd authentication.
	Token string `yaml:"token"`
	// ApidPath is the path to the apid binary. When set (along with ca_crt
	// and token), kubespand waits for secrets.API to appear in COSI state
	// then starts apid as a subprocess. This follows Talos's pattern where
	// machined's service manager waits for APIReadyCondition before starting
	// apid. If empty, apid is not managed by kubespand.
	ApidPath string `yaml:"apid_path"`
	// CertSANs are additional IPs or DNS names to include in the apid TLS
	// certificate SANs. Matches Talos machine.certSANs. Useful for
	// testing via port forwarding (e.g., "127.0.0.1").
	CertSANs []string `yaml:"cert_sans,omitempty"`
	// ListenTCP is an optional TCP address (e.g., ":50100") to expose the
	// read-only COSI API without mTLS. Useful for test harnesses and diagnostics.
	ListenTCP string `yaml:"listen_tcp"`
}

// KubePrismConfig holds KubePrism local API server load balancer settings.
type KubePrismConfig struct {
	// Enabled turns on the KubePrism local load balancer. Default: false.
	Enabled bool `yaml:"enabled"`
	// Host is the local address to bind. Default: "127.0.0.1".
	Host string `yaml:"host"`
	// Port is the local port to listen on. Default: 7445.
	Port int `yaml:"port"`
}

// ClusterConfig holds cluster identity fields (matches Talos .spec.cluster).
type ClusterConfig struct {
	// ID is the Talos cluster identity (base64). Required.
	ID string `yaml:"id"`
	// Secret is the 32-byte AES key for discovery encryption and WireGuard PSK (base64). Required.
	Secret string `yaml:"secret"`
	// Endpoint is the cluster API server URL (e.g., "https://api.allegedly.works:6443").
	// Matches Talos .spec.cluster.controlPlane.endpoint. Used by KubePrism as
	// the primary upstream before CP peers are discovered.
	Endpoint string `yaml:"endpoint"`
}

// KubespanConfig holds WireGuard interface and routing settings.
//
// TODO: 6 of 7 fields mirror kubespan.ConfigSpec. Consider switching to camelCase
// YAML tags (matching upstream) so we can embed kubespan.ConfigSpec directly and
// only add kubespand-only fields (IdentityFile). This would eliminate
// the field-by-field copying in ToConfigSpec(). Same applies to DiscoveryConfig
// vs cluster.ConfigSpec.
type KubespanConfig struct {
	// MTU for the kubespan WireGuard interface. Default: 1420.
	MTU uint32 `yaml:"mtu"`
	// ForceRouting routes all traffic through KubeSpan even when peers are down.
	ForceRouting bool `yaml:"force_routing"`
	// IdentityFile is the path to persist the WireGuard keypair.
	IdentityFile string `yaml:"identity_file"`
	// EndpointFilters control which discovered peer endpoints are accepted.
	EndpointFilters []string `yaml:"endpoint_filters,omitempty"`
	// ExtraEndpoints are additional endpoints to announce via the discovery service.
	ExtraEndpoints []netip.AddrPort `yaml:"extra_endpoints,omitempty"`
	// HarvestExtraEndpoints enables endpoint harvesting for re-announcement.
	HarvestExtraEndpoints bool `yaml:"harvest_extra_endpoints"`
	// ExcludeAdvertisedNetworks are prefixes to exclude from advertised networks.
	ExcludeAdvertisedNetworks []netip.Prefix `yaml:"exclude_advertised_networks,omitempty"`
}

// DiscoveryConfig holds discovery service settings.
type DiscoveryConfig struct {
	// Endpoint is the gRPC endpoint for the Talos discovery service.
	// Default: constants.DefaultDiscoveryServiceEndpoint
	Endpoint string `yaml:"endpoint"`
	// Insecure uses plaintext gRPC (no TLS) for the discovery service.
	Insecure bool `yaml:"insecure"`
	// MachineType is advertised to the discovery service ("worker" or "controlplane").
	MachineType string `yaml:"machine_type"`
}

// KubernetesConfig holds K8s integration settings (kubespand extension).
type KubernetesConfig struct {
	// AdvertiseNetworks enables advertising Kubernetes pod/service CIDRs via discovery.
	AdvertiseNetworks bool `yaml:"advertise_networks"`
	// KubeconfigPath is the path to a kubeconfig file for K8s API access.
	KubeconfigPath string `yaml:"kubeconfig_path"`
	// NodeName is the Kubernetes node name for this machine.
	NodeName string `yaml:"node_name"`
	// ServiceCIDRs are Kubernetes service network ranges to advertise.
	ServiceCIDRs []netip.Prefix `yaml:"service_cidrs,omitempty"`
}

// Load reads and validates a YAML config file.
func Load(path string) (*AgentConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config %s: %w", path, err)
	}

	cfg := &AgentConfig{
		Discovery: DiscoveryConfig{
			Endpoint:    constants.DefaultDiscoveryServiceEndpoint,
			MachineType: "worker",
		},
		Kubespan: KubespanConfig{
			MTU:                   constants.KubeSpanLinkMTU,
			IdentityFile:          "/var/lib/kubespan/identity.yaml",
			HarvestExtraEndpoints: true,
		},
		KubePrism: KubePrismConfig{
			Host: "127.0.0.1",
			Port: constants.DefaultKubePrismPort,
		},
	}

	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parsing config %s: %w", path, err)
	}

	if cfg.Cluster.ID == "" {
		return nil, fmt.Errorf("cluster.id is required")
	}
	if cfg.Cluster.Secret == "" {
		return nil, fmt.Errorf("cluster.secret is required")
	}
	if cfg.Kubespan.MTU < constants.KubeSpanLinkMinimumMTU {
		return nil, fmt.Errorf("kubespan.mtu must be at least %d", constants.KubeSpanLinkMinimumMTU)
	}
	if cfg.Kubernetes.AdvertiseNetworks && cfg.Kubernetes.NodeName == "" {
		return nil, fmt.Errorf("kubernetes.node_name is required when kubernetes.advertise_networks is true")
	}
	if cfg.KubePrism.Enabled {
		if cfg.Cluster.Endpoint == "" {
			return nil, fmt.Errorf("cluster.endpoint is required when kubeprism.enabled is true")
		}
		if _, err := url.Parse(cfg.Cluster.Endpoint); err != nil {
			return nil, fmt.Errorf("cluster.endpoint is not a valid URL: %w", err)
		}
	}
	if cfg.Discovery.MachineType != "worker" {
		return nil, fmt.Errorf("discovery.machine_type must be \"worker\" (kubespand only supports worker mode, got %q)", cfg.Discovery.MachineType)
	}
	if (cfg.Api.CACrt != "") != (cfg.Api.Token != "") {
		return nil, fmt.Errorf("api.ca_crt and api.token must both be set or both be empty")
	}
	if cfg.Api.CACrt != "" {
		block, _ := pem.Decode([]byte(cfg.Api.CACrt))
		if block == nil {
			return nil, fmt.Errorf("api.ca_crt is not valid PEM")
		}
	}

	return cfg, nil
}

// ToConfigSpec converts agent config to upstream kubespan.ConfigSpec for COSI injection.
func (ac *AgentConfig) ToConfigSpec() kubespan.ConfigSpec {
	return kubespan.ConfigSpec{
		Enabled:                     true,
		ClusterID:                   ac.Cluster.ID,
		SharedSecret:                ac.Cluster.Secret,
		ForceRouting:                ac.Kubespan.ForceRouting,
		MTU:                         ac.Kubespan.MTU,
		EndpointFilters:             ac.Kubespan.EndpointFilters,
		HarvestExtraEndpoints:       ac.Kubespan.HarvestExtraEndpoints,
		ExtraEndpoints:              ac.Kubespan.ExtraEndpoints,
		AdvertiseKubernetesNetworks: ac.Kubernetes.AdvertiseNetworks,
		ExcludeAdvertisedNetworks:   ac.Kubespan.ExcludeAdvertisedNetworks,
	}
}

// ToClusterConfigSpec converts agent config to upstream cluster.ConfigSpec for COSI injection.
// Normalizes the discovery endpoint URL to host:port for gRPC (matching Talos's ConfigController).
func (ac *AgentConfig) ToClusterConfigSpec() (cluster.ConfigSpec, error) {
	secretBytes, err := base64.StdEncoding.DecodeString(ac.Cluster.Secret)
	if err != nil {
		return cluster.ConfigSpec{}, fmt.Errorf("decoding cluster.secret: %w", err)
	}
	// Validate AES key length.
	if _, err := aes.NewCipher(secretBytes); err != nil {
		return cluster.ConfigSpec{}, fmt.Errorf("invalid AES key from cluster.secret: %w", err)
	}

	// Normalize endpoint URL to host:port for gRPC.
	endpoint := ac.Discovery.Endpoint
	insecure := ac.Discovery.Insecure
	if u, err := url.ParseRequestURI(endpoint); err == nil && u.Scheme != "" {
		host := u.Hostname()
		port := u.Port()
		if port == "" {
			if u.Scheme == "http" {
				port = "80"
			} else {
				port = "443"
			}
		}
		endpoint = net.JoinHostPort(host, port)
		if u.Scheme == "http" {
			insecure = true
		}
	}

	return cluster.ConfigSpec{
		DiscoveryEnabled:        true,
		RegistryServiceEnabled:  true,
		ServiceEndpoint:         endpoint,
		ServiceEndpointInsecure: insecure,
		ServiceEncryptionKey:    secretBytes,
		ServiceClusterID:        ac.Cluster.ID,
	}, nil
}
