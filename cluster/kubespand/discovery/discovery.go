// Package discovery handles communication with the Talos discovery service.
package discovery

import (
	"context"
	"crypto/aes"
	"crypto/tls"
	"fmt"
	"net"
	"net/netip"
	"time"

	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"github.com/siderolabs/talos/pkg/machinery/client/dialer"
	"github.com/siderolabs/talos/pkg/machinery/config/machine"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"go.uber.org/zap"
	"google.golang.org/grpc"
)

const discoveryTTL = 30 * time.Minute

// Manager handles communication with the Talos discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
type Manager struct {
	client   *discoveryclient.Client
	notifyCh chan struct{}
	logger   *zap.Logger
}

// NewManager creates a new discovery manager.
//
// The discovery client encrypts all affiliate data with AES-GCM using the
// shared secret as the key. The discovery service never sees plaintext node data.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run)
func NewManager(cfg *cluster.ConfigSpec, affiliateID string, logger *zap.Logger) (*Manager, error) {
	cipherBlock, err := aes.NewCipher(cfg.ServiceEncryptionKey)
	if err != nil {
		return nil, fmt.Errorf("AES cipher from cluster.secret: %w", err)
	}

	// Use passthrough:/// to bypass gRPC's built-in DNS resolver. The dns:///
	// resolver does SRV lookups (_grpcs._tcp.<host>) which hang on Tailscale
	// MagicDNS. With passthrough, the custom context dialer handles DNS via
	// Go's standard net package. See debug/kubespand-grpc-dns-magicdns.md.
	endpoint := cfg.ServiceEndpoint

	// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
	tlsConfigFunc := func() *tls.Config {
		return &tls.Config{MinVersion: tls.VersionTLS12}
	}

	opts := discoveryclient.Options{
		Cipher:        cipherBlock,
		Endpoint:      "passthrough:///" + endpoint,
		ClusterID:     cfg.ServiceClusterID,
		AffiliateID:   affiliateID,
		TTL:           discoveryTTL,
		ClientVersion: "kubespand/0.1.0",
	}
	if cfg.ServiceEndpointInsecure {
		opts.Insecure = true
	} else {
		opts.TLSConfig = tlsConfigFunc
		opts.DialOptions = []grpc.DialOption{
			grpc.WithContextDialer(dialer.DynamicProxyDialerWithTLSConfig(tlsConfigFunc)),
		}
	}

	client, err := discoveryclient.NewClient(opts)
	if err != nil {
		return nil, fmt.Errorf("creating discovery client: %w", err)
	}

	return &Manager{
		client:   client,
		notifyCh: make(chan struct{}, 1),
		logger:   logger,
	}, nil
}

// NotifyCh returns the channel that receives notifications when the peer list changes.
func (dm *Manager) NotifyCh() <-chan struct{} {
	return dm.notifyCh
}

// GetPublicIP returns this node's public IP as reported by the discovery service.
// Returns nil before the initial Hello completes.
func (dm *Manager) GetPublicIP() []byte {
	return dm.client.GetPublicIP()
}

// Run starts the discovery client event loop. Blocks until ctx is cancelled.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run, client.Run)
func (dm *Manager) Run(ctx context.Context) error {
	return dm.client.Run(ctx, dm.logger, dm.notifyCh)
}

// PublishAffiliate announces the local affiliate to the discovery service.
// The affiliate is serialized from the cluster.AffiliateSpec produced by the
// upstream LocalAffiliateController.
// otherEndpoints are harvested endpoints from EndpointController for re-announcement.
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbAffiliate, pbEndpoints)
func (dm *Manager) PublishAffiliate(spec *cluster.AffiliateSpec, otherEndpoints []discoveryclient.Endpoint) error {
	affiliate := &discoveryclient.Affiliate{
		Affiliate: &clientpb.Affiliate{
			NodeId:          spec.NodeID,
			Hostname:        spec.Hostname,
			Nodename:        spec.Nodename,
			MachineType:     spec.MachineType.String(),
			OperatingSystem: spec.OperatingSystem,
			Addresses:       marshalAddrs(spec.Addresses),
		},
	}

	// KubeSpan data.
	if spec.KubeSpan.PublicKey != "" {
		addrBytes, _ := spec.KubeSpan.Address.MarshalBinary()
		affiliate.Affiliate.Kubespan = &clientpb.KubeSpan{
			PublicKey:                  spec.KubeSpan.PublicKey,
			Address:                    addrBytes,
			AdditionalAddresses:        prefixesToPB(spec.KubeSpan.AdditionalAddresses),
			ExcludeAdvertisedAddresses: prefixesToPB(spec.KubeSpan.ExcludeAdvertisedNetworks),
		}
		affiliate.Endpoints = addrPortsToPB(spec.KubeSpan.Endpoints)
	}

	// Control plane data.
	if spec.ControlPlane != nil {
		affiliate.Affiliate.ControlPlane = &clientpb.ControlPlane{
			ApiServerPort: uint32(spec.ControlPlane.APIServerPort),
		}
	}

	return dm.client.SetLocalData(affiliate, otherEndpoints)
}

// DeleteLocalAffiliate removes this node's affiliate data from the discovery service.
// Called on shutdown to clean up immediately rather than waiting for TTL expiry.
func (dm *Manager) DeleteLocalAffiliate() {
	dm.client.DeleteLocalAffiliate()
}

// GetAffiliates returns discovered peers as a map from affiliate ID (public key)
// to cluster.AffiliateSpec. Endpoints are pre-filtered by the announcing side.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (specAffiliate)
func (dm *Manager) GetAffiliates() map[string]cluster.AffiliateSpec {
	rawAffiliates := dm.client.GetAffiliates()
	result := make(map[string]cluster.AffiliateSpec, len(rawAffiliates))

	for _, aff := range rawAffiliates {
		if aff.Affiliate == nil || aff.Affiliate.Kubespan == nil {
			continue
		}
		ks := aff.Affiliate.Kubespan
		if ks.PublicKey == "" {
			continue
		}

		machineType, _ := machine.ParseType(aff.Affiliate.MachineType)

		spec := cluster.AffiliateSpec{
			NodeID:          aff.Affiliate.NodeId,
			Hostname:        aff.Affiliate.Hostname,
			Nodename:        aff.Affiliate.Nodename,
			OperatingSystem: aff.Affiliate.OperatingSystem,
			MachineType:     machineType,
			KubeSpan: cluster.KubeSpanAffiliateSpec{
				PublicKey: ks.PublicKey,
			},
		}

		// Parse KubeSpan address.
		if len(ks.Address) > 0 {
			var addr netip.Addr
			if err := addr.UnmarshalBinary(ks.Address); err == nil {
				spec.KubeSpan.Address = addr
			}
		}

		// Parse additional addresses (networks to route via this peer).
		for _, ap := range ks.AdditionalAddresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ap.Ip); err == nil {
				spec.KubeSpan.AdditionalAddresses = append(spec.KubeSpan.AdditionalAddresses, netip.PrefixFrom(ip, int(ap.Bits)))
			}
		}

		// Parse excluded advertised addresses.
		for _, ap := range ks.ExcludeAdvertisedAddresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ap.Ip); err == nil {
				spec.KubeSpan.ExcludeAdvertisedNetworks = append(spec.KubeSpan.ExcludeAdvertisedNetworks, netip.PrefixFrom(ip, int(ap.Bits)))
			}
		}

		// Parse control plane data (API server port for CP nodes).
		if aff.Affiliate.ControlPlane != nil {
			spec.ControlPlane = &cluster.ControlPlane{
				APIServerPort: int(aff.Affiliate.ControlPlane.ApiServerPort),
			}
		}

		// Parse node addresses.
		for _, addrBytes := range aff.Affiliate.Addresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(addrBytes); err == nil {
				spec.Addresses = append(spec.Addresses, ip)
			}
		}

		// Parse endpoints (announcing side already applied endpoint filters).
		for _, ep := range aff.Endpoints {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ep.Ip); err == nil {
				spec.KubeSpan.Endpoints = append(spec.KubeSpan.Endpoints, netip.AddrPortFrom(ip, uint16(ep.Port)))
			}
		}

		result[ks.PublicKey] = spec
	}

	return result
}

// RoutedNodeAddresses returns all routable IP addresses from non-loopback,
// non-kubespan interfaces. Used by NodeMetadataController to populate
// network.NodeAddress, and by ManagerController to add them to the kubespan
// interface (so the kernel accepts reply packets arriving on kubespan).
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/local_affiliate.go
// which sets spec.Addresses from NodeAddressRoutedID.
func RoutedNodeAddresses() []netip.Addr {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}

	var addrs []netip.Addr
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if iface.Flags&net.FlagUp == 0 {
			continue
		}
		// Skip the kubespan WireGuard interface itself.
		if iface.Name == "kubespan" {
			continue
		}

		ifAddrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range ifAddrs {
			ipNet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			addr, ok := netip.AddrFromSlice(ipNet.IP)
			if !ok {
				continue
			}
			addr = addr.Unmap()
			if addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsLinkLocalMulticast() {
				continue
			}
			addrs = append(addrs, addr)
		}
	}
	return addrs
}

// marshalAddrs converts IP addresses to binary form for protobuf.
func marshalAddrs(addrs []netip.Addr) [][]byte {
	result := make([][]byte, 0, len(addrs))
	for _, addr := range addrs {
		b, err := addr.MarshalBinary()
		if err == nil {
			result = append(result, b)
		}
	}
	return result
}

// addrPortsToPB converts AddrPort slice to protobuf Endpoint messages.
func addrPortsToPB(endpoints []netip.AddrPort) []*clientpb.Endpoint {
	if len(endpoints) == 0 {
		return nil
	}
	result := make([]*clientpb.Endpoint, 0, len(endpoints))
	for _, ep := range endpoints {
		ipBytes, _ := ep.Addr().MarshalBinary()
		result = append(result, &clientpb.Endpoint{
			Ip:   ipBytes,
			Port: uint32(ep.Port()),
		})
	}
	return result
}

// prefixesToPB converts netip.Prefix slices to protobuf IPPrefix messages.
func prefixesToPB(prefixes []netip.Prefix) []*clientpb.IPPrefix {
	if len(prefixes) == 0 {
		return nil
	}
	result := make([]*clientpb.IPPrefix, 0, len(prefixes))
	for _, p := range prefixes {
		ipBytes, err := p.Addr().MarshalBinary()
		if err != nil {
			continue
		}
		result = append(result, &clientpb.IPPrefix{
			Ip:   ipBytes,
			Bits: uint32(p.Bits()),
		})
	}
	return result
}
