package iron_proxy

import (
	"net/netip"
	"testing"

	"github.com/ironsh/iron-proxy/internal/dnsguard"
	"github.com/ironsh/iron-proxy/internal/proxy"
	"github.com/ironsh/iron-proxy/internal/transform"
)

func TestDirectBazelImports(t *testing.T) {
	guard, err := dnsguard.New(dnsguard.DefaultDenyCIDRs)
	if err != nil {
		t.Fatal(err)
	}
	if !guard.IsDenied(netip.MustParseAddr("127.0.0.1")) {
		t.Fatal("upstream loopback guard did not run")
	}
	var options proxy.Options
	var transformer transform.Transformer
	t.Logf("compiled original internal packages from Agentplane: %T, %T", options, transformer)
}

func TestIronDefaultAddressPolicy(t *testing.T) {
	guard, err := dnsguard.New(dnsguard.DefaultDenyCIDRs)
	if err != nil {
		t.Fatal(err)
	}
	for address, denied := range map[string]bool{
		"127.0.0.1":       true,
		"169.254.169.254": true,
		"10.0.0.1":        false,
		"169.254.1.2":     false,
		"100.64.0.1":      false,
	} {
		if got := guard.IsDenied(netip.MustParseAddr(address)); got != denied {
			t.Errorf("default guard denies %s = %v, want %v", address, got, denied)
		}
	}
}
