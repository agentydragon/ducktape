package kubespan_test

import (
	"net/netip"
	"testing"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	kubespanadapter "github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
)

// Tests ported from talos/internal/app/machined/pkg/adapters/kubespan/peer_status_test.go

func TestCalculateState_NoEndpointChange(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-10 * time.Minute),
		LastHandshakeTime:  time.Now().Add(-10 * time.Minute),
		LastUsedEndpoint:   netip.MustParseAddrPort("1.2.3.4:51820"),
	}
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
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
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
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
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
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
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
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
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
	if ps.State != kubespan.PeerStateDown {
		t.Errorf("expected DOWN, got %s", ps.State)
	}
}

func TestCalculateState_NoEndpointEverSet(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{
		LastEndpointChange: time.Now().Add(-30 * time.Second),
		LastHandshakeTime:  time.Now().Add(-60 * time.Second),
	}
	kubespanadapter.PeerStatusSpec(ps).CalculateState()
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
			sinceHandshake:      2 * kubespanadapter.PeerDownInterval,
			sinceEndpointChange: kubespanadapter.EndpointConnectionTimeout / 2,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUnknown,
		},
		{
			name:                "just_changed_connected",
			sinceHandshake:      0,
			sinceEndpointChange: kubespanadapter.EndpointConnectionTimeout / 2,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "mid_range_connected",
			sinceHandshake:      0,
			sinceEndpointChange: kubespanadapter.EndpointConnectionTimeout + 1,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "mid_range_failed",
			sinceHandshake:      2 * kubespanadapter.EndpointConnectionTimeout,
			sinceEndpointChange: kubespanadapter.EndpointConnectionTimeout + 1,
			hasEndpoint:         true,
			want:                kubespan.PeerStateDown,
		},
		{
			name:                "established_up",
			sinceHandshake:      kubespanadapter.PeerDownInterval / 2,
			sinceEndpointChange: kubespanadapter.PeerDownInterval + 1,
			hasEndpoint:         true,
			want:                kubespan.PeerStateUp,
		},
		{
			name:                "no_endpoint_set",
			sinceHandshake:      time.Hour,
			sinceEndpointChange: time.Hour,
			hasEndpoint:         false,
			want:                kubespan.PeerStateUnknown,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ps := &kubespan.PeerStatusSpec{
				LastHandshakeTime:  time.Now().Add(-tt.sinceHandshake),
				LastEndpointChange: time.Now().Add(-tt.sinceEndpointChange),
			}
			if tt.hasEndpoint {
				ps.LastUsedEndpoint = netip.MustParseAddrPort("1.2.3.4:51820")
			}
			kubespanadapter.PeerStatusSpec(ps).CalculateStateWithDurations(tt.sinceHandshake, tt.sinceEndpointChange)
			if ps.State != tt.want {
				t.Errorf("got %s, want %s", ps.State, tt.want)
			}
		})
	}
}

func TestShouldChangeEndpoint(t *testing.T) {
	ps := &kubespan.PeerStatusSpec{State: kubespan.PeerStateDown, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if !kubespanadapter.PeerStatusSpec(ps).ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=true for DOWN peer")
	}

	ps = &kubespan.PeerStatusSpec{State: kubespan.PeerStateUnknown}
	if !kubespanadapter.PeerStatusSpec(ps).ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=true with no endpoint")
	}

	ps = &kubespan.PeerStatusSpec{State: kubespan.PeerStateUp, LastUsedEndpoint: netip.MustParseAddrPort("1.2.3.4:51820")}
	if kubespanadapter.PeerStatusSpec(ps).ShouldChangeEndpoint() {
		t.Error("expected ShouldChangeEndpoint=false for UP peer")
	}
}

func TestPickNewEndpoint(t *testing.T) {
	ep1 := netip.MustParseAddrPort("1.1.1.1:51820")
	ep2 := netip.MustParseAddrPort("2.2.2.2:51820")
	ep3 := netip.MustParseAddrPort("3.3.3.3:51820")
	endpoints := []netip.AddrPort{ep1, ep2, ep3}

	// Zero status, no endpoints → zero.
	ps := &kubespan.PeerStatusSpec{}
	got := kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(nil)
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}

	// Zero status → first endpoint.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}
	kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(got)

	// After ep1 → ep2.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(endpoints)
	if got != ep2 {
		t.Errorf("expected %s, got %s", ep2, got)
	}
	kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(got)

	// After ep2 → ep3.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(endpoints)
	if got != ep3 {
		t.Errorf("expected %s, got %s", ep3, got)
	}
	kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(got)

	// After ep3 → wraps to ep1.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(endpoints)
	if got != ep1 {
		t.Errorf("expected %s, got %s", ep1, got)
	}
	kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(got)

	// Single endpoint, already using it → can't rotate.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint([]netip.AddrPort{ep1})
	if got.IsValid() {
		t.Errorf("expected zero, got %s", got)
	}

	// Single endpoint, different from current → can rotate.
	got = kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint([]netip.AddrPort{ep2})
	if got != ep2 {
		t.Errorf("expected %s, got %s", ep2, got)
	}
}
