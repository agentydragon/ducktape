package main

import (
	"net/netip"
	"time"
)

// PeerState represents the health state of a KubeSpan peer.
// Ref: talos/pkg/machinery/resources/kubespan/peerstate_string.go
type PeerState string

const (
	PeerStateUnknown PeerState = "unknown"
	PeerStateUp      PeerState = "up"
	PeerStateDown    PeerState = "down"
)

// Timeouts for peer state machine.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go
const (
	// EndpointConnectionTimeout is how long to wait for a handshake after an
	// endpoint change before declaring the peer down.
	EndpointConnectionTimeout = 15 * time.Second

	// PeerDownInterval is how long since the last handshake before a peer is
	// considered down (regardless of endpoint changes).
	PeerDownInterval = 275 * time.Second
)

// PeerStatus tracks the live state of a single KubeSpan peer.
// Ref: talos/pkg/machinery/resources/kubespan/peer_status.go (PeerStatusSpec)
type PeerStatus struct {
	Label              string
	Endpoint           netip.AddrPort
	LastUsedEndpoint   netip.AddrPort
	LastEndpointChange time.Time
	LastHandshakeTime  time.Time
	TransmitBytes      uint64
	ReceiveBytes       uint64
	State              PeerState
}

// CalculateState computes the peer state from current time.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (CalculateState)
func (ps *PeerStatus) CalculateState() {
	now := time.Now()
	sinceHandshake := now.Sub(ps.LastHandshakeTime)
	sinceEndpointChange := now.Sub(ps.LastEndpointChange)
	ps.calculateStateWithDurations(sinceHandshake, sinceEndpointChange)
}

// calculateStateWithDurations computes the peer state from explicit durations (testable).
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
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go
//
//	(CalculateStateWithDurations)
func (ps *PeerStatus) calculateStateWithDurations(sinceHandshake, sinceEndpointChange time.Duration) {
	switch {
	case sinceEndpointChange > PeerDownInterval:
		// Long time since endpoint change — judge purely by handshake freshness.
		if sinceHandshake < PeerDownInterval {
			ps.State = PeerStateUp
		} else {
			ps.State = PeerStateDown
		}

	case sinceEndpointChange < EndpointConnectionTimeout:
		// Just changed endpoint, give it time to connect.
		if sinceHandshake < sinceEndpointChange {
			// Handshake happened after the endpoint change.
			ps.State = PeerStateUp
		} else {
			ps.State = PeerStateUnknown
		}

	default:
		// Between 15s and 275s since endpoint change.
		if sinceHandshake < sinceEndpointChange {
			ps.State = PeerStateUp
		} else {
			ps.State = PeerStateDown
		}
	}

	// If state is DOWN but we've never set an endpoint, treat as UNKNOWN.
	if ps.State == PeerStateDown && !ps.LastUsedEndpoint.IsValid() {
		ps.State = PeerStateUnknown
	}
}

// ShouldChangeEndpoint returns true when the peer needs a new endpoint.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (ShouldChangeEndpoint)
func (ps *PeerStatus) ShouldChangeEndpoint() bool {
	return ps.State == PeerStateDown || !ps.LastUsedEndpoint.IsValid()
}

// PickNewEndpoint selects the next endpoint to try, round-robin style.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (PickNewEndpoint)
func (ps *PeerStatus) PickNewEndpoint(endpoints []netip.AddrPort) netip.AddrPort {
	if len(endpoints) == 0 {
		return netip.AddrPort{}
	}

	if !ps.LastUsedEndpoint.IsValid() {
		return endpoints[0]
	}

	// Find current endpoint in list and advance to next.
	for i, ep := range endpoints {
		if ep == ps.LastUsedEndpoint {
			next := endpoints[(i+1)%len(endpoints)]
			// Don't rotate if there's only one endpoint and it's the same.
			if next == ps.LastUsedEndpoint {
				return netip.AddrPort{}
			}
			return next
		}
	}

	// Current endpoint not in list, start from the beginning.
	return endpoints[0]
}

// UpdateEndpoint records that we're trying a new endpoint.
// Ref: talos/internal/app/machined/pkg/adapters/kubespan/peer_status.go (UpdateEndpoint)
func (ps *PeerStatus) UpdateEndpoint(endpoint netip.AddrPort) {
	ps.Endpoint = endpoint
	ps.LastUsedEndpoint = endpoint
	ps.LastEndpointChange = time.Now()
	ps.State = PeerStateUnknown
}
