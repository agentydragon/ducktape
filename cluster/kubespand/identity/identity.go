// Package identity handles KubeSpan node identity (WireGuard keypair persistence).
//
// Key generation and address derivation are in the upstream adapter
// (peerstate package, pulled from Talos adapters/kubespan/identity.go).
// LoadOrCreate is kubespand-only (Talos uses STATE partition).
// MAC detection uses the upstream HardwareAddrController (network.HardwareAddr resource).
package identity

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"gopkg.in/yaml.v3"

	kubespanadapter "github.com/siderolabs/talos/internal/app/machined/pkg/adapters/kubespan"
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
