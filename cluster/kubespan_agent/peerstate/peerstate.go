// Package peerstate provides adapter functions for KubeSpan peer status management.
//
// Functions match Talos internal/app/machined/pkg/adapters/kubespan/peer_status.go.
// Uses upstream kubespan.PeerStatusSpec directly.
package peerstate

import (
	"net/netip"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// Timeouts for peer state machine.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/wireguard.go (PeerDownInterval)
const (
	// EndpointConnectionTimeout is how long to wait for a handshake after an
	// endpoint change before declaring the peer down.
	EndpointConnectionTimeout = 15 * time.Second

	// PeerDownInterval is how long since the last handshake before a peer is
	// considered down. Computed from WireGuard whitepaper constants:
	// RekeyAfterTime (120s) + KeepaliveTimeout (10s) + RekeyTimeout (5s) +
	// RekeyAttemptTime (90s) + KeepaliveTimeout (10s) + padding (40s) = 275s.
	// Talos computes: (180 + 5 + 90) * time.Second.
	PeerDownInterval = (180 + 5 + 90) * time.Second
)

// CalculateState computes the peer state from current time.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (CalculateState)
func CalculateState(spec *kubespan.PeerStatusSpec) {
	now := time.Now()
	sinceHandshake := now.Sub(spec.LastHandshakeTime)
	sinceEndpointChange := now.Sub(spec.LastEndpointChange)
	CalculateStateWithDurations(spec, sinceHandshake, sinceEndpointChange)
}

// CalculateStateWithDurations computes the peer state from explicit durations (testable).
//
// State machine (from Talos source):
//
//	Timeline: ──T0──────T0+15s──────────T0+275s──>
//
//	Case 1: sinceEndpointChange > PeerDownInterval
//	  handshake < PeerDownInterval → UP
//	  else → DOWN
//
//	Case 2: sinceEndpointChange < EndpointConnectionTimeout
//	  handshake happened after endpoint change → UP
//	  else → UNKNOWN (still connecting)
//
//	Case 3: between 15s and 275s since endpoint change
//	  handshake happened after endpoint change → UP
//	  else → DOWN
//
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (CalculateStateWithDurations)
func CalculateStateWithDurations(spec *kubespan.PeerStatusSpec, sinceHandshake, sinceEndpointChange time.Duration) {
	switch {
	case sinceEndpointChange > PeerDownInterval:
		if sinceHandshake < PeerDownInterval {
			spec.State = kubespan.PeerStateUp
		} else {
			spec.State = kubespan.PeerStateDown
		}

	case sinceEndpointChange < EndpointConnectionTimeout:
		if sinceHandshake < sinceEndpointChange {
			spec.State = kubespan.PeerStateUp
		} else {
			spec.State = kubespan.PeerStateUnknown
		}

	default:
		if sinceHandshake < sinceEndpointChange {
			spec.State = kubespan.PeerStateUp
		} else {
			spec.State = kubespan.PeerStateDown
		}
	}

	if spec.State == kubespan.PeerStateDown && !spec.LastUsedEndpoint.IsValid() {
		spec.State = kubespan.PeerStateUnknown
	}
}

// ShouldChangeEndpoint returns true when the peer needs a new endpoint.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (ShouldChangeEndpoint)
func ShouldChangeEndpoint(spec *kubespan.PeerStatusSpec) bool {
	return spec.State == kubespan.PeerStateDown || !spec.LastUsedEndpoint.IsValid()
}

// PickNewEndpoint selects the next endpoint to try, round-robin style.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (PickNewEndpoint)
func PickNewEndpoint(spec *kubespan.PeerStatusSpec, endpoints []netip.AddrPort) netip.AddrPort {
	if len(endpoints) == 0 {
		return netip.AddrPort{}
	}

	if !spec.LastUsedEndpoint.IsValid() {
		return endpoints[0]
	}

	for i, ep := range endpoints {
		if ep == spec.LastUsedEndpoint {
			next := endpoints[(i+1)%len(endpoints)]
			if next == spec.LastUsedEndpoint {
				return netip.AddrPort{}
			}
			return next
		}
	}

	return endpoints[0]
}

// UpdateEndpoint records that we're trying a new endpoint.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (UpdateEndpoint)
func UpdateEndpoint(spec *kubespan.PeerStatusSpec, endpoint netip.AddrPort) {
	spec.Endpoint = endpoint
	spec.LastUsedEndpoint = endpoint
	spec.LastEndpointChange = time.Now()
	spec.State = kubespan.PeerStateUnknown
}

// UpdateFromWireguard updates the peer status from WireGuard peer data.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (UpdateFromWireguard)
func UpdateFromWireguard(spec *kubespan.PeerStatusSpec, peer wgtypes.Peer) {
	spec.LastHandshakeTime = peer.LastHandshakeTime

	if peer.Endpoint != nil {
		spec.Endpoint = peer.Endpoint.AddrPort()
	}

	spec.TransmitBytes = int64(peer.TransmitBytes)
	spec.ReceiveBytes = int64(peer.ReceiveBytes)
}
