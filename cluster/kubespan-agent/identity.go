package main

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

// Identity holds the node's KubeSpan identity (WireGuard keypair + derived addresses).
// Ref: talos/pkg/machinery/resources/kubespan/identity.go (IdentitySpec)
type Identity struct {
	PrivateKey string `json:"private_key"`
	PublicKey  string `json:"public_key"`
	Subnet     string `json:"subnet"`  // ULA /64 prefix
	Address    string `json:"address"` // EUI-64 /128 address
}

// LoadOrCreateIdentity loads an existing identity from disk, or generates a new one.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/identity.go (IdentityController)
func LoadOrCreateIdentity(path string, clusterID string) (*Identity, error) {
	id, err := loadIdentity(path)
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

	id = &Identity{
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

func loadIdentity(path string) (*Identity, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var id Identity
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
func (id *Identity) UpdateAddress(clusterID string, mac net.HardwareAddr) error {
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
func (id *Identity) ParsedAddress() (netip.Prefix, error) {
	return netip.ParsePrefix(id.Address)
}

// ParsedSubnet returns the node's KubeSpan subnet as a netip.Prefix.
func (id *Identity) ParsedSubnet() (netip.Prefix, error) {
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

// DetectMAC finds the first non-loopback, non-virtual NIC's MAC address.
// Ref: talos/pkg/machinery/resources/network/hardware_addr.go (FirstHardwareAddr)
func DetectMAC() (net.HardwareAddr, error) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, fmt.Errorf("listing interfaces: %w", err)
	}

	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if len(iface.HardwareAddr) == 0 {
			continue
		}
		// Skip common virtual interface prefixes.
		if iface.Name == "docker0" || iface.Name == "br0" {
			continue
		}
		return iface.HardwareAddr, nil
	}

	return nil, errors.New("no suitable network interface found")
}
