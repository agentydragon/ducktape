// Package identity handles KubeSpan node identity (WireGuard keypair + derived addresses).
package identity

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"

	"github.com/mdlayher/netx/eui64"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go4.org/netipx"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// Spec holds the node's KubeSpan identity (WireGuard keypair + derived addresses).
// Ref: talos/pkg/machinery/resources/kubespan/identity.go (IdentitySpec)
type Spec struct {
	PrivateKey string `json:"private_key"`
	PublicKey  string `json:"public_key"`
	Subnet     string `json:"subnet"`  // ULA /64 prefix
	Address    string `json:"address"` // EUI-64 /128 address
}

// DeepCopy returns a deep copy of the Spec.
func (id Spec) DeepCopy() Spec {
	return id
}

// LoadOrCreate loads an existing identity from disk, or generates a new one.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/identity.go (IdentityController)
func LoadOrCreate(path string, clusterID string) (*Spec, error) {
	id, err := load(path)
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("reading identity %s: %w", path, err)
	}

	// Generate new keypair.
	// Ref: talos/internal/app/machined/pkg/adapters/kubespan/identity.go (GenerateKey)
	key, err := wgtypes.GeneratePrivateKey()
	if err != nil {
		return nil, fmt.Errorf("generating WireGuard key: %w", err)
	}

	id = &Spec{
		PrivateKey: key.String(),
		PublicKey:  key.PublicKey().String(),
	}

	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, fmt.Errorf("creating identity directory: %w", err)
	}

	data, err := json.MarshalIndent(id, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("marshaling identity: %w", err)
	}

	if err := os.WriteFile(path, data, 0600); err != nil {
		return nil, fmt.Errorf("writing identity %s: %w", path, err)
	}

	return id, nil
}

func load(path string) (*Spec, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var id Spec
	if err := json.Unmarshal(data, &id); err != nil {
		return nil, fmt.Errorf("parsing identity: %w", err)
	}
	if id.PrivateKey == "" || id.PublicKey == "" {
		return nil, fmt.Errorf("identity missing keys")
	}
	return &id, nil
}

// UpdateAddress computes the node's KubeSpan ULA address from the cluster ID and
// first NIC MAC address.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/identity.go (UpdateAddress)
func (id *Spec) UpdateAddress(clusterID string, mac net.HardwareAddr) error {
	subnet := network.ULAPrefix(clusterID, network.ULAKubeSpan)
	id.Subnet = subnet.String()

	addr, err := wgEUI64(subnet, mac)
	if err != nil {
		return err
	}
	id.Address = addr.String()

	return nil
}

// ParsedAddress returns the node's KubeSpan address as a netip.Prefix.
func (id *Spec) ParsedAddress() (netip.Prefix, error) {
	return netip.ParsePrefix(id.Address)
}

// ParsedSubnet returns the node's KubeSpan subnet as a netip.Prefix.
func (id *Spec) ParsedSubnet() (netip.Prefix, error) {
	return netip.ParsePrefix(id.Subnet)
}

// wgEUI64 computes an EUI-64 address within the given prefix from a MAC address.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/identity.go (wgEUI64)
func wgEUI64(prefix netip.Prefix, mac net.HardwareAddr) (netip.Prefix, error) {
	if !prefix.IsValid() {
		return netip.Prefix{}, errors.New("cannot calculate IP from zero prefix")
	}

	stdIP, err := eui64.ParseMAC(netipx.PrefixIPNet(prefix).IP, mac)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("EUI-64 from MAC: %w", err)
	}

	ip, ok := netipx.FromStdIP(stdIP)
	if !ok {
		return netip.Prefix{}, fmt.Errorf("converting EUI-64 result %q", stdIP)
	}

	return netip.PrefixFrom(ip, ip.BitLen()), nil
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
		// Physical NICs have a /sys/class/net/<name>/device symlink pointing
		// to the PCI/USB device. Virtual interfaces (bridges, veth, tun, etc.)
		// do not. This is more robust than name-based filtering.
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
