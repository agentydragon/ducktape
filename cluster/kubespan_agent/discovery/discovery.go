// Package discovery handles communication with the Talos discovery service.
package discovery

import (
	"context"
	"crypto/aes"
	"crypto/tls"
	"encoding/base64"
	"fmt"
	"net/netip"
	"os"
	"runtime"
	"strings"
	"time"

	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/agentconfig"
)

const discoveryTTL = 30 * time.Minute

// Manager handles communication with the Talos discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
type Manager struct {
	client          *discoveryclient.Client
	notifyCh        chan struct{}
	logger          *zap.Logger
	endpointFilters []endpointFilter
}

// endpointFilter is a parsed CIDR filter for peer endpoints.
type endpointFilter struct {
	prefix netip.Prefix
	deny   bool // true if prefixed with "!"
}

// NewManager creates a new discovery manager.
//
// The discovery client encrypts all affiliate data with AES-GCM using the
// shared secret as the key. The discovery service never sees plaintext node data.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run)
func NewManager(cfg *agentconfig.AgentConfig, affiliateID string, logger *zap.Logger) (*Manager, error) {
	secretBytes, err := base64.StdEncoding.DecodeString(cfg.SharedSecret)
	if err != nil {
		return nil, fmt.Errorf("decoding shared_secret: %w", err)
	}

	cipherBlock, err := aes.NewCipher(secretBytes)
	if err != nil {
		return nil, fmt.Errorf("AES cipher from shared_secret: %w", err)
	}

	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if cfg.InsecureDiscovery {
		tlsConfig.InsecureSkipVerify = true //nolint:gosec // intentional for self-hosted testing
	}

	client, err := discoveryclient.NewClient(discoveryclient.Options{
		Cipher:        cipherBlock,
		Endpoint:      cfg.DiscoveryEndpoint,
		ClusterID:     cfg.ClusterID,
		AffiliateID:   affiliateID,
		TTL:           discoveryTTL,
		ClientVersion: "kubespand/0.1.0",
		TLSConfig:     tlsConfig,
	})
	if err != nil {
		return nil, fmt.Errorf("creating discovery client: %w", err)
	}

	// Parse endpoint filters.
	// Ref: talos/pkg/machinery/resources/kubespan/config.go (EndpointFilters)
	var filters []endpointFilter
	for _, f := range cfg.EndpointFilters {
		deny := false
		cidr := f
		if strings.HasPrefix(cidr, "!") {
			deny = true
			cidr = cidr[1:]
		}
		prefix, parseErr := netip.ParsePrefix(cidr)
		if parseErr != nil {
			return nil, fmt.Errorf("invalid endpoint_filter %q: %w", f, parseErr)
		}
		filters = append(filters, endpointFilter{prefix: prefix, deny: deny})
	}

	return &Manager{
		client:          client,
		notifyCh:        make(chan struct{}, 1),
		logger:          logger,
		endpointFilters: filters,
	}, nil
}

// NotifyCh returns the channel that receives notifications when the peer list changes.
func (dm *Manager) NotifyCh() <-chan struct{} {
	return dm.notifyCh
}

// Run starts the discovery client event loop. Blocks until ctx is cancelled.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run, client.Run)
func (dm *Manager) Run(ctx context.Context) error {
	return dm.client.Run(ctx, dm.logger, dm.notifyCh)
}

// PublishLocal announces this node's affiliate data to the discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbAffiliate, pbEndpoints)
func (dm *Manager) PublishLocal(cfg *agentconfig.AgentConfig, id *kubespan.IdentitySpec, listenPort int) error {
	addrBytes, _ := id.Address.Addr().MarshalBinary()

	hostname, _ := os.Hostname()

	// Build endpoint list from extra_endpoints config.
	// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbEndpoints)
	// TODO: implement client.GetPublicIP() for external endpoint discovery
	// TODO: implement pbOtherEndpoints() to re-announce harvested endpoints from EndpointController
	var endpoints []*clientpb.Endpoint
	for _, addrPort := range cfg.ExtraEndpoints {
		ipBytes, _ := addrPort.Addr().MarshalBinary()
		endpoints = append(endpoints, &clientpb.Endpoint{
			Ip:   ipBytes,
			Port: uint32(addrPort.Port()),
		})
	}

	affiliate := &discoveryclient.Affiliate{
		Affiliate: &clientpb.Affiliate{
			NodeId:          id.PublicKey,
			Hostname:        hostname,
			Nodename:        hostname,
			MachineType:     cfg.MachineType,
			OperatingSystem: runtime.GOOS + "/" + runtime.GOARCH + " (kubespand)",
			Kubespan: &clientpb.KubeSpan{
				PublicKey: id.PublicKey,
				Address:   addrBytes,
			},
		},
		Endpoints: endpoints,
	}

	return dm.client.SetLocalData(affiliate, nil)
}

// DeleteLocalAffiliate removes this node's affiliate data from the discovery service.
// Called on shutdown to clean up immediately rather than waiting for TTL expiry.
// TODO: align DeleteLocalAffiliate with Talos MachineResetSignal pattern
func (dm *Manager) DeleteLocalAffiliate() {
	dm.client.DeleteLocalAffiliate()
}

// GetPeers returns the current list of discovered peers as a map from public key
// to PeerSpecSpec. Endpoints are filtered according to the configured EndpointFilters.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (specAffiliate)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go (PeerSpecController)
func (dm *Manager) GetPeers() map[string]kubespan.PeerSpecSpec {
	affiliates := dm.client.GetAffiliates()
	peers := make(map[string]kubespan.PeerSpecSpec, len(affiliates))

	for _, aff := range affiliates {
		if aff.Affiliate == nil || aff.Affiliate.Kubespan == nil {
			continue
		}
		ks := aff.Affiliate.Kubespan
		if ks.PublicKey == "" {
			continue
		}

		peer := kubespan.PeerSpecSpec{
			Label: aff.Affiliate.Nodename,
		}

		// Parse KubeSpan address.
		if len(ks.Address) > 0 {
			var addr netip.Addr
			if err := addr.UnmarshalBinary(ks.Address); err == nil {
				peer.Address = addr
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

		// Parse and filter endpoints.
		for _, ep := range aff.Endpoints {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(ep.Ip); err == nil {
				addrPort := netip.AddrPortFrom(ip, uint16(ep.Port))
				if dm.endpointAllowed(ip) {
					peer.Endpoints = append(peer.Endpoints, addrPort)
				}
			}
		}

		peers[ks.PublicKey] = peer
	}

	return peers
}

// endpointAllowed checks if an endpoint IP passes the configured endpoint filters.
func (dm *Manager) endpointAllowed(addr netip.Addr) bool {
	if len(dm.endpointFilters) == 0 {
		return true
	}
	for _, f := range dm.endpointFilters {
		if f.prefix.Contains(addr) {
			return !f.deny
		}
	}
	return false
}
