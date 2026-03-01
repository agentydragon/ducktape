package main

import (
	"context"
	"crypto/aes"
	"crypto/tls"
	"encoding/base64"
	"fmt"
	"net/netip"
	"os"
	"time"

	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"go.uber.org/zap"
)

const discoveryTTL = 30 * time.Minute

// Peer represents a discovered KubeSpan peer.
// Ref: talos/pkg/machinery/resources/kubespan/peer_spec.go (PeerSpecSpec)
type Peer struct {
	PublicKey  string
	Address   netip.Addr     // KubeSpan ULA /128
	Endpoints []netip.AddrPort
	AllowedIPs []netip.Prefix
	Label     string // node name for logging
}

// DiscoveryManager handles communication with the Talos discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
type DiscoveryManager struct {
	client   *discoveryclient.Client
	notifyCh chan struct{}
	logger   *zap.Logger
}

// NewDiscoveryManager creates a new discovery manager.
//
// The discovery client encrypts all affiliate data with AES-GCM using the cluster
// secret as the key. The discovery service never sees plaintext node data.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run)
func NewDiscoveryManager(cfg *Config, affiliateID string, logger *zap.Logger) (*DiscoveryManager, error) {
	secretBytes, err := base64.StdEncoding.DecodeString(cfg.ClusterSecret)
	if err != nil {
		return nil, fmt.Errorf("decoding cluster_secret: %w", err)
	}

	cipherBlock, err := aes.NewCipher(secretBytes)
	if err != nil {
		return nil, fmt.Errorf("AES cipher from cluster_secret: %w", err)
	}

	client, err := discoveryclient.NewClient(discoveryclient.Options{
		Cipher:        cipherBlock,
		Endpoint:      cfg.DiscoveryEndpoint,
		ClusterID:     cfg.ClusterID,
		AffiliateID:   affiliateID,
		TTL:           discoveryTTL,
		ClientVersion: "kubespand/0.1.0",
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12},
	})
	if err != nil {
		return nil, fmt.Errorf("creating discovery client: %w", err)
	}

	return &DiscoveryManager{
		client:   client,
		notifyCh: make(chan struct{}, 1),
		logger:   logger,
	}, nil
}

// NotifyCh returns the channel that receives notifications when the peer list changes.
func (dm *DiscoveryManager) NotifyCh() <-chan struct{} {
	return dm.notifyCh
}

// Run starts the discovery client event loop. Blocks until ctx is cancelled.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run, client.Run)
func (dm *DiscoveryManager) Run(ctx context.Context) error {
	return dm.client.Run(ctx, dm.logger, dm.notifyCh)
}

// PublishLocal announces this node's affiliate data to the discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbAffiliate)
func (dm *DiscoveryManager) PublishLocal(id *Identity, listenPort int) error {
	addr, err := id.ParsedAddress()
	if err != nil {
		return fmt.Errorf("parsing identity address: %w", err)
	}

	hostname, _ := os.Hostname()

	addrBytes, _ := addr.Addr().MarshalBinary()

	affiliate := &discoveryclient.Affiliate{
		Affiliate: &clientpb.Affiliate{
			NodeId:          id.PublicKey, // Use public key as node ID (unique per identity)
			Hostname:        hostname,
			Nodename:        hostname,
			MachineType:     "worker",
			OperatingSystem: "Linux (kubespand)",
			Kubespan: &clientpb.KubeSpan{
				PublicKey: id.PublicKey,
				Address:   addrBytes,
			},
		},
	}

	return dm.client.SetLocalData(affiliate, nil)
}

// GetPeers returns the current list of discovered peers, converted from
// discovery affiliates to our internal Peer type.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (specAffiliate)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go (PeerSpecController)
func (dm *DiscoveryManager) GetPeers() []Peer {
	affiliates := dm.client.GetAffiliates()
	peers := make([]Peer, 0, len(affiliates))

	for _, aff := range affiliates {
		if aff.Affiliate == nil || aff.Affiliate.Kubespan == nil {
			continue
		}
		ks := aff.Affiliate.Kubespan
		if ks.PublicKey == "" {
			continue
		}

		peer := Peer{
			PublicKey: ks.PublicKey,
			Label:    aff.Affiliate.Nodename,
		}

		// Parse KubeSpan address.
		if len(ks.Address) > 0 {
			var addr netip.Addr
			if err := addr.UnmarshalBinary(ks.Address); err == nil {
				peer.Address = addr
				// The node's /128 is always an allowed IP.
				peer.AllowedIPs = append(peer.AllowedIPs, netip.PrefixFrom(addr, addr.BitLen()))
			}
		}

		// Parse additional addresses (networks to route via this peer).
		for _, ap := range ks.AdditionalAddresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ap.Ip); err == nil {
				peer.AllowedIPs = append(peer.AllowedIPs, netip.PrefixFrom(ip, int(ap.Bits)))
			}
		}

		// Parse node addresses (pod IPs etc).
		for _, addrBytes := range aff.Affiliate.Addresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(addrBytes); err == nil {
				peer.AllowedIPs = append(peer.AllowedIPs, netip.PrefixFrom(ip, ip.BitLen()))
			}
		}

		// Parse endpoints.
		for _, ep := range aff.Endpoints {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ep.Ip); err == nil {
				peer.Endpoints = append(peer.Endpoints, netip.AddrPortFrom(ip, uint16(ep.Port)))
			}
		}

		peers = append(peers, peer)
	}

	return peers
}
