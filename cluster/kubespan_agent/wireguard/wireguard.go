// Package wireguard manages the KubeSpan WireGuard interface and its peers.
package wireguard

import (
	"encoding/base64"
	"fmt"
	"net"
	"net/netip"
	"time"

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
func NewManager(privateKeyBase64 string, clusterSecret string, listenPort, mtu int) (*Manager, error) {
	client, err := wgctrl.New()
	if err != nil {
		return nil, fmt.Errorf("wgctrl client: %w", err)
	}

	privKey, err := wgtypes.ParseKey(privateKeyBase64)
	if err != nil {
		return nil, fmt.Errorf("parsing private key: %w", err)
	}

	// Cluster secret is used as preshared key for all peers.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
	secretBytes, err := base64.StdEncoding.DecodeString(clusterSecret)
	if err != nil {
		return nil, fmt.Errorf("decoding cluster_secret for PSK: %w", err)
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
		// Create the interface.
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

	// Configure WireGuard.
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

	// Assign the IPv6 ULA address.
	addr := &netlink.Addr{
		IPNet: prefixToIPNet(address),
	}
	if err := netlink.AddrReplace(link, addr); err != nil {
		return fmt.Errorf("assigning address %s to %s: %w", address, constants.KubeSpanLinkName, err)
	}

	// Bring the interface up.
	if err := netlink.LinkSetUp(link); err != nil {
		return fmt.Errorf("bringing up %s: %w", constants.KubeSpanLinkName, err)
	}

	return nil
}

// Peer is the configuration for a single WireGuard peer.
type Peer struct {
	PublicKey  string
	Endpoint   netip.AddrPort
	AllowedIPs []netip.Prefix
}

// ConfigurePeers sets the WireGuard peers on the kubespan interface.
// Each peer gets the cluster secret as preshared key and a 25s keepalive.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (WireGuardPeer config)
func (wm *Manager) ConfigurePeers(peers []Peer) error {
	wgPeers := make([]wgtypes.PeerConfig, 0, len(peers))

	for _, p := range peers {
		pubKey, err := wgtypes.ParseKey(p.PublicKey)
		if err != nil {
			continue // skip peers with invalid keys
		}

		peerCfg := wgtypes.PeerConfig{
			PublicKey:                   pubKey,
			PresharedKey:                &wm.psk,
			PersistentKeepaliveInterval: durationPtr(constants.KubeSpanDefaultPeerKeepalive),
			ReplaceAllowedIPs:           true,
			AllowedIPs:                  prefixesToIPNets(p.AllowedIPs),
		}

		if p.Endpoint.IsValid() {
			peerCfg.Endpoint = addrPortToUDPAddr(p.Endpoint)
		}

		wgPeers = append(wgPeers, peerCfg)
	}

	return wm.client.ConfigureDevice(constants.KubeSpanLinkName, wgtypes.Config{
		ReplacePeers: true,
		Peers:        wgPeers,
	})
}

// PeerInfo holds live WireGuard data for a peer.
type PeerInfo struct {
	LastHandshakeTime time.Time
	Endpoint          netip.AddrPort
	TransmitBytes     uint64
	ReceiveBytes      uint64
}

// GetPeerHandshakes queries the WireGuard device for current peer handshake times.
// Returns a map of public key → last handshake time.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (wgDevice.Peers loop)
func (wm *Manager) GetPeerHandshakes() (map[string]PeerInfo, error) {
	dev, err := wm.client.Device(constants.KubeSpanLinkName)
	if err != nil {
		return nil, fmt.Errorf("querying %s device: %w", constants.KubeSpanLinkName, err)
	}

	result := make(map[string]PeerInfo, len(dev.Peers))
	for _, p := range dev.Peers {
		var endpoint netip.AddrPort
		if p.Endpoint != nil {
			endpoint = p.Endpoint.AddrPort()
		}
		result[p.PublicKey.String()] = PeerInfo{
			LastHandshakeTime: p.LastHandshakeTime,
			Endpoint:          endpoint,
			TransmitBytes:     uint64(p.TransmitBytes),
			ReceiveBytes:      uint64(p.ReceiveBytes),
		}
	}

	return result, nil
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

// Helper conversions.

func prefixToIPNet(p netip.Prefix) *net.IPNet {
	return &net.IPNet{
		IP:   p.Addr().AsSlice(),
		Mask: net.CIDRMask(p.Bits(), p.Addr().BitLen()),
	}
}

func prefixesToIPNets(prefixes []netip.Prefix) []net.IPNet {
	nets := make([]net.IPNet, len(prefixes))
	for i, p := range prefixes {
		nets[i] = *prefixToIPNet(p)
	}
	return nets
}

func addrPortToUDPAddr(ap netip.AddrPort) *net.UDPAddr {
	return &net.UDPAddr{
		IP:   ap.Addr().AsSlice(),
		Port: int(ap.Port()),
	}
}

func durationPtr(d time.Duration) *time.Duration {
	return &d
}
