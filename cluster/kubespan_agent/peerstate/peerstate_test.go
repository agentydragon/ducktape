package peerstate_test

import (
	"net/netip"
	"testing"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
)

// Tests ported from talos/internal/app/machined/pkg/adapters/kubespan/peer_status_test.go

func TestCalculateState_NoEndpointChange(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-10 * time.Minute),
		LastHandshakeTime:  time.Now().Add(-10 * time.Minute),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateDown {
		t.Errorf("expected DOWN, got %s", ps.State)
	}
}

func TestCalculateState_RecentHandshake(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-10 * time.Minute),
		LastHandshakeTime:  time.Now().Add(-30 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateUp {
		t.Errorf("expected UP, got %s", ps.State)
	}
}

func TestCalculateState_JustChangedEndpoint(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-5 * time.Second),
		LastHandshakeTime:  time.Now().Add(-10 * time.Minute),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateUnknown {
		t.Errorf("expected UNKNOWN, got %s", ps.State)
	}
}

func TestCalculateState_JustChangedEndpointWithHandshake(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-5 * time.Second),
		LastHandshakeTime:  time.Now().Add(-2 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateUp {
		t.Errorf("expected UP, got %s", ps.State)
	}
}

func TestCalculateState_EndpointChangeMidRange(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-30 * time.Second),
		LastHandshakeTime:  time.Now().Add(-60 * time.Second),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateDown {
		t.Errorf("expected DOWN, got %s", ps.State)
	}
}

func TestCalculateState_NoEndpointEverSet(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-30 * time.Second),
		LastHandshakeTime:  time.Now().Add(-60 * time.Second),
	}
	peerstate.CalculateState(ps)
	if ps.State != kubespan.PeerStateUnknown {
		t.Errorf("expected UNKNOWN (no endpoint ever set), got %s", ps.State)
	}
}

func TestCalculateStateWithDurations(t *testing.T) {
	tests := []struct {
		name                string
		sinceHandshake      time.Duration
		sinceEndpointChange time.Duration
		hasEndpoint         bool
		want                kubespan.PeerState
	}{
		{
			name:                "long_stable_up",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 10 * time.Minute,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "long_stable_down",
			sinceHandshake:      6 * time.Minute,
			sinceEndpointChange: 10 * time.Minute,
			hasEndpoint:         true,
			want:                kubespan.PeerStateDown,
		},
		{
			name:                "just_changed_waiting",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 10 * time.Second,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUnknown,
		},
		{
			name:                "just_changed_connected",
			sinceHandshake:      5 * time.Second,
			sinceEndpointChange: 10 * time.Second,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "mid_range_connected",
			sinceHandshake:      10 * time.Second,
			sinceEndpointChange: 30 * time.Second,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "mid_range_failed",
			sinceHandshake:      60 * time.Second,
			sinceEndpointChange: 30 * time.Second,
			hasEndpoint:         true,
			want:                kubespan.PeerStateDown,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ps := &kubespan.PeerStatusSpec{}
			if tt.hasEndpoint {
				ps.LastUsedEndpoint = netip.MustParseAddrPort("1.2.3.4:51820")
			}
			peerstate.CalculateStateWithDurations(ps, tt.sinceHandshake, tt.sinceEndpointChange)
			if ps.State != tt.want {
				t.Errorf("got %s, want %s", ps.State, tt.want)
			}
		})
	}
}

func TestShouldChangeEndpoint(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{State: kubespan.PeerStateDown, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if !peerstate.ShouldChangeEndpoint(ps) {
		t.Error("expected ShouldChangeEndpoint=true for DOWN peer")
	}

	ps = &kubespan.PeerStatusSpec{State: kubespan.PeerStateUnknown}
	if !peerstate.ShouldChangeEndpoint(ps) {
		t.Error("expected ShouldChangeEndpoint=true with no endpoint")
	}

	ps = &kubespan.PeerStatusSpec{State: kubespan.PeerStateUp, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if peerstate.ShouldChangeEndpoint(ps) {
		t.Error("expected ShouldChangeEndpoint=false for UP peer")
	}
}

func TestPickNewEndpoint(t *testing.T) {
	ep1 := netip.MustParseAddrPort("1.1.1.1:51820")
	ep2 := netip.MustParseAddrPort("2.2.2.2:51820")
	ep3 := netip.MustParseAddrPort("3.3.3.3:51820")
	endpoints := []netip.AddrPort{ep1, ep2, ep3}

	ps := &kubespan.PeerStatusSpec{}
	got := peerstate.PickNewEndpoint(ps, endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}

	ps = &kubespan.PeerStatusSpec{LastUsedEndpoint: ep1}
	got = peerstate.PickNewEndpoint(ps, endpoints)
	if got != ep2 {
		t.Errorf("expected %s, got %s", ep2, got)
	}

	ps = &kubespan.PeerStatusSpec{LastUsedEndpoint: ep3}
	got = peerstate.PickNewEndpoint(ps, endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}

	ps = &kubespan.PeerStatusSpec{LastUsedEndpoint: ep1}
	got = peerstate.PickNewEndpoint(ps, []netip.AddrPort{ep1})
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}

	ps = &kubespan.PeerStatusSpec{}
	got = peerstate.PickNewEndpoint(ps, nil)
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}
}
