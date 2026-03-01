package main

import (
	"net/netip"
	"testing"
	"time"
)

// Tests ported from talos/internal/app/machined/pkg/adapters/kubespan/peer_status_test.go

func TestCalculateState_NoEndpointChange(t *testing.T) {
	// Long time since endpoint change, no handshake → DOWN
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-10 * time.Minute),
		LastHandshakeTime:  time.Now().Add(-10 * time.Minute),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	ps.CalculateState()
	if ps.State != PeerStateDown {
		t.Errorf("expected DOWN, got %s", ps.State)
	}
}

func TestCalculateState_RecentHandshake(t *testing.T) {
	// Long time since endpoint change, recent handshake → UP
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-10 * time.Minute),
		LastHandshakeTime:  time.Now().Add(-30 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	ps.CalculateState()
	if ps.State != PeerStateUp {
		t.Errorf("expected UP, got %s", ps.State)
	}
}

func TestCalculateState_JustChangedEndpoint(t *testing.T) {
	// Just changed endpoint (< 15s), no handshake yet → UNKNOWN
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-5 * time.Second),
		LastHandshakeTime:  time.Now().Add(-10 * time.Minute),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	ps.CalculateState()
	if ps.State != PeerStateUnknown {
		t.Errorf("expected UNKNOWN, got %s", ps.State)
	}
}

func TestCalculateState_JustChangedEndpointWithHandshake(t *testing.T) {
	// Just changed endpoint (< 15s), handshake happened after change → UP
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-5 * time.Second),
		LastHandshakeTime:  time.Now().Add(-2 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	ps.CalculateState()
	if ps.State != PeerStateUp {
		t.Errorf("expected UP, got %s", ps.State)
	}
}

func TestCalculateState_EndpointChangeMidRange(t *testing.T) {
	// 30s since endpoint change, no handshake since → DOWN
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-30 * time.Second),
		LastHandshakeTime:  time.Now().Add(-60 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	ps.CalculateState()
	if ps.State != PeerStateDown {
		t.Errorf("expected DOWN, got %s", ps.State)
	}
}

func TestCalculateState_NoEndpointEverSet(t *testing.T) {
	// DOWN but no endpoint ever set → UNKNOWN
	ps := &PeerStatus{
		LastEndpointChange: time.Now().Add(-30 * time.Second),
		LastHandshakeTime:  time.Now().Add(-60 * time.Second),
	}
	ps.CalculateState()
	if ps.State != PeerStateUnknown {
		t.Errorf("expected UNKNOWN (no endpoint ever set), got %s", ps.State)
	}
}

func TestCalculateStateWithDurations(t *testing.T) {
	tests := []struct {
		name                string
		sinceHandshake      time.Duration
		sinceEndpointChange time.Duration
		hasEndpoint         bool
		want                PeerState
	}{
		{
			name:                "long_stable_up",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 10 * time.Minute,
			hasEndpoint:         true,
			want:                PeerStateUp,
		},
		{
			name:                "long_stable_down",
			sinceHandshake:      6 * time.Minute,
			sinceEndpointChange: 10 * time.Minute,
			hasEndpoint:         true,
			want:                PeerStateDown,
		},
		{
			name:                "just_changed_waiting",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 10 * time.Second,
			hasEndpoint:         true,
			want:                PeerStateUnknown,
		},
		{
			name:                "just_changed_connected",
			sinceHandshake:      5 * time.Second,
			sinceEndpointChange: 10 * time.Second,
			hasEndpoint:         true,
			want:                PeerStateUp,
		},
		{
			name:                "mid_range_connected",
			sinceHandshake:      10 * time.Second,
			sinceEndpointChange: 30 * time.Second,
			hasEndpoint:         true,
			want:                PeerStateUp,
		},
		{
			name:                "mid_range_failed",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 30 * time.Second,
			hasEndpoint:         true,
			want:                PeerStateDown,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ps := &PeerStatus{}
			if tt.hasEndpoint {
				ps.LastUsedEndpoint = netip.MustParseAddrPort("1.2.3.4:51820")
			}
			ps.calculateStateWithDurations(tt.sinceHandshake, tt.sinceEndpointChange)
			if ps.State != tt.want {
				t.Errorf("got %s, want %s", ps.State, tt.want)
			}
		})
	}
}

func TestShouldChangeEndpoint(t *testing.T) {
	// DOWN → should change
	ps := &PeerStatus{State: PeerStateDown, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if !ps.ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=true for DOWN peer")
	}

	// No endpoint set → should change
	ps = &PeerStatus{State: PeerStateUnknown}
	if !ps.ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=true with no endpoint")
	}

	// UP → should not change
	ps = &PeerStatus{State: PeerStateUp, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if ps.ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=false for UP peer")
	}
}

func TestPickNewEndpoint(t *testing.T) {
	ep1 := netip.MustParseAddrPort("1.1.1.1:51820")
	ep2 := netip.MustParseAddrPort("2.2.2.2:51820")
	ep3 := netip.MustParseAddrPort("3.3.3.3:51820")
	endpoints := []netip.AddrPort{ep1, ep2, ep3}

	// No last endpoint → pick first
	ps := &PeerStatus{}
	got := ps.PickNewEndpoint(endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}

	// Last was ep1 → pick ep2
	ps = &PeerStatus{LastUsedEndpoint: ep1}
	got = ps.PickNewEndpoint(endpoints)
	if got != ep2 {
		t.Errorf("expected %s, got %s", ep2, got)
	}

	// Last was ep3 → wrap to ep1
	ps = &PeerStatus{LastUsedEndpoint: ep3}
	got = ps.PickNewEndpoint(endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}

	// Single endpoint, already set → return zero (don't rotate)
	ps = &PeerStatus{LastUsedEndpoint: ep1}
	got = ps.PickNewEndpoint([]netip.AddrPort{ep1})
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}

	// Empty list → return zero
	ps = &PeerStatus{}
	got = ps.PickNewEndpoint(nil)
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}
}
