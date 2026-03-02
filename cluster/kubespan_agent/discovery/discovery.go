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
	"slices"
	"time"

	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"github.com/siderolabs/talos/pkg/machinery/config/machine"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/agentconfig"
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

// Run starts the discovery client event loop. Blocks until ctx is cancelled.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (Run, client.Run)
func (dm *Manager) Run(ctx context.Context) error {
	return dm.client.Run(ctx, dm.logger, dm.notifyCh)
}

// PublishLocal announces this node's affiliate data to the discovery service.
// otherEndpoints are harvested endpoints from EndpointController for re-announcement.
// additionalAddresses are Kubernetes network prefixes (PodCIDRs + ServiceCIDRs) to advertise.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbAffiliate, pbEndpoints, pbOtherEndpoints)
func (dm *Manager) PublishLocal(cfg *agentconfig.AgentConfig, id *kubespan.IdentitySpec, listenPort int, otherEndpoints []discoveryclient.Endpoint, additionalAddresses []netip.Prefix) error {
	addrBytes, _ := id.Address.Addr().MarshalBinary()

	hostname, _ := os.Hostname()

	// Build endpoint list from extra_endpoints config.
	var endpoints []*clientpb.Endpoint
	for _, addrPort := range cfg.ExtraEndpoints {
		ipBytes, _ := addrPort.Addr().MarshalBinary()
		endpoints = append(endpoints, &clientpb.Endpoint{
			Ip:   ipBytes,
			Port: uint32(addrPort.Port()),
		})
	}

	// Add public IP from discovery service (external endpoint discovery).
	if pubIPBytes := dm.client.GetPublicIP(); len(pubIPBytes) > 0 {
		if pubIP, ok := netip.AddrFromSlice(pubIPBytes); ok {
			pubEndpoint := netip.AddrPortFrom(pubIP, uint16(listenPort))
			pubEPBytes, _ := pubEndpoint.Addr().MarshalBinary()
			pbEP := &clientpb.Endpoint{Ip: pubEPBytes, Port: uint32(pubEndpoint.Port())}
			// Avoid duplicates with extra_endpoints.
			found := false
			for _, ep := range endpoints {
				if slices.Equal(ep.Ip, pbEP.Ip) && ep.Port == pbEP.Port {
					found = true
					break
				}
			}
			if !found {
				endpoints = append(endpoints, pbEP)
			}
		}
	}

	affiliate := &discoveryclient.Affiliate{
		Affiliate: &clientpb.Affiliate{
			NodeId:          id.PublicKey,
			Hostname:        hostname,
			Nodename:        hostname,
			MachineType:     cfg.MachineType,
			OperatingSystem: runtime.GOOS + "/" + runtime.GOARCH + " (kubespand)",
			Kubespan: &clientpb.KubeSpan{
				PublicKey:           id.PublicKey,
				Address:             addrBytes,
				AdditionalAddresses: prefixesToPBAddresses(additionalAddresses),
			},
		},
		Endpoints: endpoints,
	}

	return dm.client.SetLocalData(affiliate, otherEndpoints)
}

// DeleteLocalAffiliate removes this node's affiliate data from the discovery service.
// Called on shutdown to clean up immediately rather than waiting for TTL expiry.
func (dm *Manager) DeleteLocalAffiliate() {
	dm.client.DeleteLocalAffiliate()
}

// GetAffiliates returns discovered peers as a map from affiliate ID (public key)
// to cluster.AffiliateSpec. Endpoints are returned unfiltered; filtering is done
// by the PeerSpecController.
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

		// Parse node addresses.
		for _, addrBytes := range aff.Affiliate.Addresses {
			var ip netip.Addr
			if err := ip.UnmarshalBinary(addrBytes); err == nil {
				spec.Addresses = append(spec.Addresses, ip)
			}
		}

		// Parse endpoints (no filtering — PeerSpecController handles that).
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

// prefixesToPBAddresses converts netip.Prefix slices to protobuf IPPrefix messages
// for the discovery service. Matches the format parsed in GetAffiliates().
func prefixesToPBAddresses(prefixes []netip.Prefix) []*clientpb.IPPrefix {
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
