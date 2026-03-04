package kubespan

import (
	"net/netip"
	"testing"
)

func TestParseEndpointFilters(t *testing.T) {
	tests := []struct {
		name    string
		raw     []string
		wantLen int
		// First filter's deny flag, if any.
		wantFirstDeny bool
	}{
		{name: "empty", raw: nil, wantLen: 0},
		{name: "allow", raw: []string{"10.0.0.0/8"}, wantLen: 1, wantFirstDeny: false},
		{name: "deny", raw: []string{"!192.168.0.0/16"}, wantLen: 1, wantFirstDeny: true},
		{name: "invalid_skipped", raw: []string{"not-a-cidr", "10.0.0.0/8"}, wantLen: 1},
		{name: "mixed", raw: []string{"!100.64.0.0/10", "0.0.0.0/0"}, wantLen: 2, wantFirstDeny: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := parseEndpointFilters(tt.raw)
			if len(got) != tt.wantLen {
				t.Fatalf("len = %d, want %d", len(got), tt.wantLen)
			}
			if tt.wantLen > 0 && got[0].deny != tt.wantFirstDeny {
				t.Errorf("first filter deny = %v, want %v", got[0].deny, tt.wantFirstDeny)
			}
		})
	}
}

func TestEndpointAllowed(t *testing.T) {
	ep := netip.MustParseAddrPort("10.5.0.1:51820")

	tests := []struct {
		name    string
		filters []endpointFilter
		want    bool
	}{
		{name: "no_filters_allows_all", filters: nil, want: true},
		{
			name: "allow_match",
			filters: []endpointFilter{
				{prefix: netip.MustParsePrefix("10.0.0.0/8"), deny: false},
			},
			want: true,
		},
		{
			name: "deny_match",
			filters: []endpointFilter{
				{prefix: netip.MustParsePrefix("10.0.0.0/8"), deny: true},
			},
			want: false,
		},
		{
			name: "first_match_wins_deny_before_allow",
			filters: []endpointFilter{
				{prefix: netip.MustParsePrefix("10.5.0.0/16"), deny: true},
				{prefix: netip.MustParsePrefix("10.0.0.0/8"), deny: false},
			},
			want: false,
		},
		{
			name: "first_match_wins_allow_before_deny",
			filters: []endpointFilter{
				{prefix: netip.MustParsePrefix("10.5.0.0/16"), deny: false},
				{prefix: netip.MustParsePrefix("10.0.0.0/8"), deny: true},
			},
			want: true,
		},
		{
			name: "no_match_default_deny",
			filters: []endpointFilter{
				{prefix: netip.MustParsePrefix("192.168.0.0/16"), deny: false},
			},
			want: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := endpointAllowed(ep, tt.filters); got != tt.want {
				t.Errorf("endpointAllowed = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestFilterEndpoints(t *testing.T) {
	eps := []netip.AddrPort{
		netip.MustParseAddrPort("10.5.0.1:51820"),
		netip.MustParseAddrPort("192.168.1.1:51820"),
		netip.MustParseAddrPort("172.16.0.1:51820"),
	}

	t.Run("no_filters_returns_all", func(t *testing.T) {
		got := filterEndpoints(eps, nil)
		if len(got) != 3 {
			t.Fatalf("len = %d, want 3", len(got))
		}
	})

	t.Run("deny_private_allow_rest", func(t *testing.T) {
		filters := parseEndpointFilters([]string{"!10.0.0.0/8", "!192.168.0.0/16", "0.0.0.0/0"})
		got := filterEndpoints(eps, filters)
		if len(got) != 1 {
			t.Fatalf("len = %d, want 1", len(got))
		}
		if got[0] != eps[2] {
			t.Errorf("got %s, want %s", got[0], eps[2])
		}
	})

	t.Run("allow_only_10_net", func(t *testing.T) {
		filters := parseEndpointFilters([]string{"10.0.0.0/8"})
		got := filterEndpoints(eps, filters)
		if len(got) != 1 {
			t.Fatalf("len = %d, want 1", len(got))
		}
		if got[0] != eps[0] {
			t.Errorf("got %s, want %s", got[0], eps[0])
		}
	})

	t.Run("empty_endpoints", func(t *testing.T) {
		filters := parseEndpointFilters([]string{"0.0.0.0/0"})
		got := filterEndpoints(nil, filters)
		if len(got) != 0 {
			t.Fatalf("len = %d, want 0", len(got))
		}
	})
}
