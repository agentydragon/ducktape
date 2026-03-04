// Package wireguard manages the KubeSpan WireGuard interface and its peers.
package wireguard

import (
	"encoding/base64"
	"fmt"
	"net"
	"net/netip"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/vishvananda/netlink"
	"golang.zx2c4.com/wireguard/wgctrl"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// Manager manages the KubeSpan WireGuard interface and its peers.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (ManagerController)
type Manager struct {
	client     *wgctrl.Client
	privateKey wgtypes.Key
	psk        wgtypes.Key
	listenPort int
	mtu        int
}

// NewManager creates a WireGuard manager.
func NewManager(privateKeyBase64 string, sharedSecret string, listenPort, mtu int) (*Manager, error) {
	client, err := wgctrl.New()
	if err != nil {
		return nil, fmt.Errorf("wgctrl client: %w", err)
	}

	privKey, err := wgtypes.ParseKey(privateKeyBase64)
	if err != nil {
		return nil, fmt.Errorf("parsing private key: %w", err)
	}

	// Shared secret is used as preshared key for all peers.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
	secretBytes, err := base64.StdEncoding.DecodeString(sharedSecret)
	if err != nil {
		return nil, fmt.Errorf("decoding shared_secret for PSK: %w", err)
	}
	if len(secretBytes) != wgtypes.KeyLen {
		return nil, fmt.Errorf("decoded shared_secret length %d, expected %d", len(secretBytes), wgtypes.KeyLen)
	}
	var pskBytes [wgtypes.KeyLen]byte
	copy(pskBytes[:], secretBytes)
	psk := wgtypes.Key(pskBytes)

	return &Manager{
		client:     client,
		privateKey: privKey,
		psk:        psk,
		listenPort: listenPort,
		mtu:        mtu,
	}, nil
}

// EnsureInterface creates the kubespan WireGuard interface if it doesn't exist,
// configures it with the private key and listen port, and assigns the node address.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (network.LinkSpec)
func (wm *Manager) EnsureInterface(address netip.Prefix) error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		wgLink := &netlink.Wireguard{
			LinkAttrs: netlink.LinkAttrs{
				Name: constants.KubeSpanLinkName,
				MTU:  wm.mtu,
			},
		}
		if err := netlink.LinkAdd(wgLink); err != nil {
			return fmt.Errorf("creating %s interface: %w", constants.KubeSpanLinkName, err)
		}
		link, err = netlink.LinkByName(constants.KubeSpanLinkName)
		if err != nil {
			return fmt.Errorf("finding created %s interface: %w", constants.KubeSpanLinkName, err)
		}
	}

	fwmark := constants.KubeSpanDefaultFirewallMark
	port := wm.listenPort
	err = wm.client.ConfigureDevice(constants.KubeSpanLinkName, wgtypes.Config{
		PrivateKey:   &wm.privateKey,
		ListenPort:   &port,
		FirewallMark: &fwmark,
	})
	if err != nil {
		return fmt.Errorf("configuring %s WireGuard: %w", constants.KubeSpanLinkName, err)
	}

	addr := &netlink.Addr{
		IPNet: prefixToIPNet(address),
	}
	if err := netlink.AddrReplace(link, addr); err != nil {
		return fmt.Errorf("assigning address %s to %s: %w", address, constants.KubeSpanLinkName, err)
	}

	if err := netlink.LinkSetUp(link); err != nil {
		return fmt.Errorf("bringing up %s: %w", constants.KubeSpanLinkName, err)
	}

	return nil
}

// PresharedKey returns the WireGuard preshared key for peer configuration.
func (wm *Manager) PresharedKey() *wgtypes.Key {
	return &wm.psk
}

// ConfigurePeers sets the WireGuard peers on the kubespan interface.
// Callers build wgtypes.PeerConfig directly using PresharedKey(), AddrPortToUDPAddr(),
// and PrefixesToIPNets() helpers.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (WireGuardPeer config)
func (wm *Manager) ConfigurePeers(peers []wgtypes.PeerConfig) error {
	return wm.client.ConfigureDevice(constants.KubeSpanLinkName, wgtypes.Config{
		ReplacePeers: true,
		Peers:        peers,
	})
}

// GetPeers queries the WireGuard device for current peer state.
// Returns the raw wgtypes.Peer list for use with peerstate.UpdateFromWireguard.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (wgDevice.Peers loop)
func (wm *Manager) GetPeers() ([]wgtypes.Peer, error) {
	dev, err := wm.client.Device(constants.KubeSpanLinkName)
	if err != nil {
		return nil, fmt.Errorf("querying %s device: %w", constants.KubeSpanLinkName, err)
	}
	return dev.Peers, nil
}

// Cleanup removes the kubespan WireGuard interface.
func (wm *Manager) Cleanup() error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		return nil // already gone
	}
	return netlink.LinkDel(link)
}

// Close releases the wgctrl client.
func (wm *Manager) Close() error {
	return wm.client.Close()
}

// PrefixesToIPNets converts netip.Prefix slices to net.IPNet slices for wgtypes.
func PrefixesToIPNets(prefixes []netip.Prefix) []net.IPNet {
	nets := make([]net.IPNet, len(prefixes))
	for i, p := range prefixes {
		nets[i] = net.IPNet{
			IP:   p.Addr().AsSlice(),
			Mask: net.CIDRMask(p.Bits(), p.Addr().BitLen()),
		}
	}
	return nets
}

// AddrPortToUDPAddr converts netip.AddrPort to *net.UDPAddr for wgtypes.
func AddrPortToUDPAddr(ap netip.AddrPort) *net.UDPAddr {
	return &net.UDPAddr{
		IP:   ap.Addr().AsSlice(),
		Port: int(ap.Port()),
	}
}

func prefixToIPNet(p netip.Prefix) *net.IPNet {
	return &net.IPNet{
		IP:   p.Addr().AsSlice(),
		Mask: net.CIDRMask(p.Bits(), p.Addr().BitLen()),
	}
}
