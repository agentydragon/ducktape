package main

import (
	"bufio"
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
	"go.uber.org/zap"
)

const discoveryTTL = 30 * time.Minute

// PeerSpecSpec represents a discovered KubeSpan peer.
// Ref: talos/pkg/machinery/resources/kubespan/peer_spec.go (PeerSpecSpec)
type PeerSpecSpec struct {
	PublicKey  string
	Address    netip.Addr // KubeSpan ULA /128
	Endpoints  []netip.AddrPort
	AllowedIPs []netip.Prefix
	Label      string // node name for logging
}

// DeepCopy returns a deep copy of the PeerSpecSpec.
func (p PeerSpecSpec) DeepCopy() PeerSpecSpec {
	cp := p
	cp.Endpoints = append([]netip.AddrPort(nil), p.Endpoints...)
	cp.AllowedIPs = append([]netip.Prefix(nil), p.AllowedIPs...)
	return cp
}

// DiscoveryManager handles communication with the Talos discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
type DiscoveryManager struct {
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

// NewDiscoveryManager creates a new discovery manager.
//
// The discovery client encrypts all affiliate data with AES-GCM using the cluster
// secret as the key. The discovery service never sees plaintext node data.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run)
func NewDiscoveryManager(cfg *ConfigSpec, affiliateID string, logger *zap.Logger) (*DiscoveryManager, error) {
	secretBytes, err := base64.StdEncoding.DecodeString(cfg.ClusterSecret)
	if err != nil {
		return nil, fmt.Errorf("decoding cluster_secret: %w", err)
	}

	cipherBlock, err := aes.NewCipher(secretBytes)
	if err != nil {
		return nil, fmt.Errorf("AES cipher from cluster_secret: %w", err)
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

	return &DiscoveryManager{
		client:          client,
		notifyCh:        make(chan struct{}, 1),
		logger:          logger,
		endpointFilters: filters,
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
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbAffiliate, pbEndpoints)
func (dm *DiscoveryManager) PublishLocal(cfg *ConfigSpec, id *IdentitySpec, listenPort int) error {
	addr, err := id.ParsedAddress()
	if err != nil {
		return fmt.Errorf("parsing identity address: %w", err)
	}

	hostname, _ := os.Hostname()

	addrBytes, _ := addr.Addr().MarshalBinary()

	// Build endpoint list from extra_endpoints config.
	// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbEndpoints)
	var endpoints []*clientpb.Endpoint
	for _, ep := range cfg.ExtraEndpoints {
		addrPort, parseErr := netip.ParseAddrPort(ep)
		if parseErr != nil {
			dm.logger.Warn("skipping invalid extra_endpoint", zap.String("endpoint", ep), zap.Error(parseErr))
			continue
		}
		ipBytes, _ := addrPort.Addr().MarshalBinary()
		endpoints = append(endpoints, &clientpb.Endpoint{
			Ip:   ipBytes,
			Port: uint32(addrPort.Port()),
		})
	}

	affiliate := &discoveryclient.Affiliate{
		Affiliate: &clientpb.Affiliate{
			NodeId:          id.PublicKey, // Use public key as node ID (unique per identity)
			Hostname:        hostname,
			Nodename:        hostname,
			MachineType:     cfg.MachineType,
			OperatingSystem: detectOS() + " (kubespand)",
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
func (dm *DiscoveryManager) DeleteLocalAffiliate() {
	dm.client.DeleteLocalAffiliate()
}

// detectOS returns a human-readable OS identifier string (without the kubespand suffix).
// On Linux, reads /etc/os-release for the distro name and version.
func detectOS() string {
	if runtime.GOOS != "linux" {
		return runtime.GOOS
	}

	f, err := os.Open("/etc/os-release")
	if err != nil {
		return "Linux"
	}
	defer f.Close()

	var name, version string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "PRETTY_NAME=") {
			return strings.Trim(strings.TrimPrefix(line, "PRETTY_NAME="), "\"")
		}
		if strings.HasPrefix(line, "NAME=") {
			name = strings.Trim(strings.TrimPrefix(line, "NAME="), "\"")
		}
		if strings.HasPrefix(line, "VERSION=") {
			version = strings.Trim(strings.TrimPrefix(line, "VERSION="), "\"")
		}
	}

	if name != "" {
		if version != "" {
			return name + " " + version
		}
		return name
	}
	return "Linux"
}

// GetPeers returns the current list of discovered peers, converted from
// discovery affiliates to our internal Peer type. Endpoints are filtered
// according to the configured EndpointFilters.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (specAffiliate)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go (PeerSpecController)
func (dm *DiscoveryManager) GetPeers() []PeerSpecSpec {
	affiliates := dm.client.GetAffiliates()
	peers := make([]PeerSpecSpec, 0, len(affiliates))

	for _, aff := range affiliates {
		if aff.Affiliate == nil || aff.Affiliate.Kubespan == nil {
			continue
		}
		ks := aff.Affiliate.Kubespan
		if ks.PublicKey == "" {
			continue
		}

		peer := PeerSpecSpec{
			PublicKey: ks.PublicKey,
			Label:     aff.Affiliate.Nodename,
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

		peers = append(peers, peer)
	}

	return peers
}

// endpointAllowed checks if an endpoint IP passes the configured endpoint filters.
// Filters are evaluated in order; the first matching filter determines the result.
// If no filter matches, the endpoint is allowed (default accept).
// Ref: talos/pkg/machinery/resources/kubespan/config.go (EndpointFilters)
func (dm *DiscoveryManager) endpointAllowed(addr netip.Addr) bool {
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
