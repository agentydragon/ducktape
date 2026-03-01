// Package main implements kubespand, a standalone KubeSpan agent for non-Talos Linux.
//
// KubeSpan is Talos Linux's built-in WireGuard mesh network. This daemon reimplements
// the node-side protocol so that non-Talos machines can join the mesh as first-class peers.
//
// Protocol reference: https://github.com/siderolabs/talos (MPL-2.0)
// Discovery service: https://github.com/siderolabs/discovery-service
// Discovery client: https://github.com/siderolabs/discovery-client
// Confirmed no standalone client: https://github.com/siderolabs/talos/discussions/10032
package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config holds the kubespand configuration.
// Ref: talos/pkg/machinery/resources/kubespan/config.go (ConfigSpec)
type Config struct {
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
}

// LoadConfig reads and validates a YAML config file.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config %s: %w", path, err)
	}

	cfg := &Config{
		DiscoveryEndpoint: "discovery.talos.dev:443",
		ListenPort:        51820,
		MTU:               1420,
		IdentityFile:      "/var/lib/kubespan/identity.json",
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
	if cfg.MTU < 1280 {
		// Ref: talos/pkg/machinery/constants/constants.go (KubeSpanLinkMinimumMTU = 1280)
		return nil, fmt.Errorf("mtu must be at least 1280")
	}

	return cfg, nil
}
