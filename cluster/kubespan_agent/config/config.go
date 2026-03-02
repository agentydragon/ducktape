// Package config holds the kubespand configuration type and loader.
//
// KubeSpan is Talos Linux's built-in WireGuard mesh network. This daemon reimplements
// the node-side protocol so that non-Talos machines can join the mesh as first-class peers.
//
// Protocol reference: https://github.com/siderolabs/talos (MPL-2.0)
// Discovery service: https://github.com/siderolabs/discovery-service
// Discovery client: https://github.com/siderolabs/discovery-client
// Confirmed no standalone client: https://github.com/siderolabs/talos/discussions/10032
package config

import (
	"fmt"
	"os"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"gopkg.in/yaml.v3"
)

// Spec holds the kubespand configuration.
// Ref: talos/pkg/machinery/resources/kubespan/config.go (ConfigSpec)
type Spec struct {
	// ClusterID is the Talos cluster identity, used for ULA prefix generation and
	// discovery service registration.
	// Extract: talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.id'
	ClusterID string `yaml:"cluster_id"`

	// ClusterSecret is the base64-encoded 32-byte key used for:
	// 1. AES-GCM encryption of discovery service data
	// 2. WireGuard preshared key for all peers
	// Extract: talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.secret'
	ClusterSecret string `yaml:"cluster_secret"`

	// DiscoveryEndpoint is the gRPC endpoint for the Talos discovery service.
	// Default: discovery.talos.dev:443
	// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
	DiscoveryEndpoint string `yaml:"discovery_endpoint"`

	// ListenPort is the UDP port for the WireGuard interface.
	// Ref: talos/pkg/machinery/constants/constants.go (KubeSpanDefaultPort = 51820)
	ListenPort int `yaml:"listen_port"`

	// MTU for the KubeSpan WireGuard interface.
	// Ref: talos/pkg/machinery/constants/constants.go (KubeSpanLinkMTU = 1420)
	MTU int `yaml:"mtu"`

	// IdentityFile is the path to persist the WireGuard keypair.
	IdentityFile string `yaml:"identity_file"`

	// ForceRouting routes all peer traffic through KubeSpan even when peers are down.
	// Ref: talos/pkg/machinery/resources/kubespan/config.go (ForceRouting)
	ForceRouting bool `yaml:"force_routing"`

	// MachineType is advertised to the discovery service. Talos uses "worker" or
	// "controlplane" — the value is informational and does not change behavior.
	// Ref: talos/pkg/machinery/api/machine/machine.go (TypeWorker, TypeControlPlane)
	MachineType string `yaml:"machine_type"`

	// ExtraEndpoints are additional endpoints to announce via the discovery service.
	// Useful for nodes behind NAT where the public IP differs from the detected IP.
	// Format: "ip:port" (e.g., "203.0.113.1:51820").
	// Ref: talos/pkg/machinery/resources/kubespan/config.go (ExtraEndpoints)
	ExtraEndpoints []string `yaml:"extra_endpoints"`

	// EndpointFilters controls which discovered peer endpoints are accepted.
	// Each entry is a CIDR prefix. Prefix with "!" to exclude.
	// Example: ["0.0.0.0/0", "!192.168.0.0/16", "::/0"]
	// Empty means accept all endpoints.
	// Ref: talos/pkg/machinery/resources/kubespan/config.go (EndpointFilters)
	EndpointFilters []string `yaml:"endpoint_filters"`

	// InsecureDiscovery disables TLS certificate verification for the discovery service.
	// Only use for testing with self-hosted discovery services using self-signed certs.
	InsecureDiscovery bool `yaml:"insecure_discovery"`
}

// DeepCopy returns a deep copy of the Spec.
func (c Spec) DeepCopy() Spec {
	cp := c
	cp.ExtraEndpoints = append([]string(nil), c.ExtraEndpoints...)
	cp.EndpointFilters = append([]string(nil), c.EndpointFilters...)
	return cp
}

// Load reads and validates a YAML config file.
func Load(path string) (*Spec, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config %s: %w", path, err)
	}

	cfg := &Spec{
		DiscoveryEndpoint: constants.DefaultDiscoveryServiceEndpoint,
		ListenPort:        constants.KubeSpanDefaultPort,
		MTU:               constants.KubeSpanLinkMTU,
		IdentityFile:      "/var/lib/kubespan/identity.json",
		MachineType:       "worker",
	}

	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parsing config %s: %w", path, err)
	}

	if cfg.ClusterID == "" {
		return nil, fmt.Errorf("cluster_id is required")
	}
	if cfg.ClusterSecret == "" {
		return nil, fmt.Errorf("cluster_secret is required")
	}
	if cfg.MTU < constants.KubeSpanLinkMinimumMTU {
		return nil, fmt.Errorf("mtu must be at least %d", constants.KubeSpanLinkMinimumMTU)
	}

	return cfg, nil
}
