// Package identity handles KubeSpan node identity (WireGuard keypair + derived addresses).
//
// Key generation and address derivation are in the upstream adapter
// (peerstate package, pulled from Talos adapters/kubespan/identity.go).
// LoadOrCreate and DetectMAC are kubespand-only (Talos uses STATE partition and COSI HardwareAddr).
package identity

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"gopkg.in/yaml.v3"

	kubespanadapter "github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
)

// LoadOrCreate loads an existing identity from disk, or generates a new one.
// The identity file uses YAML format matching Talos's kubespan-identity.yaml.
func LoadOrCreate(path string, clusterID string) (*kubespan.IdentitySpec, error) {
	spec, err := load(path)
	if err == nil {
		return spec, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("reading identity %s: %w", path, err)
	}

	spec = &kubespan.IdentitySpec{}
	if err := kubespanadapter.IdentitySpec(spec).GenerateKey(); err != nil {
		return nil, fmt.Errorf("generating WireGuard key: %w", err)
	}

	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, fmt.Errorf("creating identity directory: %w", err)
	}

	data, err := yaml.Marshal(spec)
	if err != nil {
		return nil, fmt.Errorf("marshaling identity: %w", err)
	}

	if err := os.WriteFile(path, data, 0600); err != nil {
		return nil, fmt.Errorf("writing identity %s: %w", path, err)
	}

	return spec, nil
}

func load(path string) (*kubespan.IdentitySpec, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var spec kubespan.IdentitySpec
	if err := yaml.Unmarshal(data, &spec); err != nil {
		return nil, fmt.Errorf("parsing identity: %w", err)
	}
	if spec.PrivateKey == "" || spec.PublicKey == "" {
		return nil, fmt.Errorf("identity missing keys")
	}
	return &spec, nil
}

// DetectMAC finds the first physical NIC's MAC address.
//
// Uses the same heuristic as Talos's FirstHardwareAddr: iterates interfaces
// in kernel order, skipping loopback and interfaces without a MAC. Physical
// NICs are preferred (detected via /sys/class/net/<name>/device symlink);
// falls back to the first non-loopback interface if no physical NIC is found
// (e.g., in a container).
func DetectMAC() (net.HardwareAddr, error) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, fmt.Errorf("listing interfaces: %w", err)
	}

	var firstNonLoopback net.HardwareAddr
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 || len(iface.HardwareAddr) == 0 {
			continue
		}
		if firstNonLoopback == nil {
			firstNonLoopback = iface.HardwareAddr
		}
		if _, err := os.Stat(fmt.Sprintf("/sys/class/net/%s/device", iface.Name)); err != nil {
			continue
		}
		return iface.HardwareAddr, nil
	}

	if firstNonLoopback != nil {
		return firstNonLoopback, nil
	}
	return nil, errors.New("no suitable network interface found")
}
