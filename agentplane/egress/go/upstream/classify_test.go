package upstream

import (
	"net/netip"
	"testing"
)

var loopback = []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8"), netip.MustParsePrefix("::1/128")}

func TestVerdictsMatchCPython(t *testing.T) {
	for _, row := range cpythonVerdicts {
		t.Run(row.address, func(t *testing.T) {
			address := netip.MustParseAddr(row.address)
			reachable := row.global && !row.multicast
			if got := Reachable(address, nil, false); got != reachable {
				t.Errorf("Reachable(%s) = %v, want %v", address, got, reachable)
			}
			if got := ClusterInternal(address); got != row.internal {
				t.Errorf("ClusterInternal(%s) = %v, want %v", address, got, row.internal)
			}
			if got := Reachable(address, nil, true); got != (reachable || row.internal) {
				t.Errorf("Reachable(%s, internal) = %v, want %v", address, got, reachable || row.internal)
			}
		})
	}
}

func TestSpecialPurposeRangesAreNotReachable(t *testing.T) {
	for _, address := range []string{
		"10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1", "169.254.169.254", "127.0.0.1", "0.0.0.0", "224.0.0.1",
		"::1", "fc00::1", "fe80::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
	} {
		if Reachable(netip.MustParseAddr(address), nil, false) {
			t.Errorf("%s is reachable", address)
		}
	}
}

func TestGlobalUnicastIsReachable(t *testing.T) {
	for _, address := range []string{"140.82.121.4", "2606:50c0:8000::153"} {
		if !Reachable(netip.MustParseAddr(address), nil, false) {
			t.Errorf("%s is not reachable", address)
		}
	}
}

func TestExemptionCoversTheIPv4MappedForm(t *testing.T) {
	if !Reachable(netip.MustParseAddr("::ffff:127.0.0.1"), loopback, false) {
		t.Error("an exemption of 127.0.0.0/8 does not cover ::ffff:127.0.0.1")
	}
	if Reachable(netip.MustParseAddr("::ffff:10.0.0.1"), loopback, false) {
		t.Error("an exemption of loopback covers ::ffff:10.0.0.1")
	}
}

func TestExemptionCoversAZonedAddress(t *testing.T) {
	zoned := netip.MustParseAddr("fe80::1%eth0")
	if !Reachable(zoned, []netip.Prefix{netip.MustParsePrefix("fe80::/10")}, false) || Reachable(zoned, nil, false) {
		t.Errorf("a zoned %s is not judged by its address alone", zoned)
	}
}

// What lets an in-cluster Service be reached through the proxy at all.
func TestARuleDeclaringItsHostInternalReachesAPrivateAddress(t *testing.T) {
	for _, address := range []string{"10.1.2.3", "172.16.0.1", "192.168.1.1", "fd00::1"} {
		parsed := netip.MustParseAddr(address)
		if Reachable(parsed, nil, false) || !Reachable(parsed, nil, true) {
			t.Errorf("%s: internal does not alone lift the refusal", address)
		}
	}
}

// `internal` means the cluster network. Loopback is the sidecar and the runner's own listeners,
// and link-local is whatever the node's metadata service is; neither is a Service.
func TestDeclaringAHostInternalNeverReachesTheSandboxsOwnInterfaces(t *testing.T) {
	for _, address := range []string{"127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "::1", "fe80::1", "ff02::1"} {
		if Reachable(netip.MustParseAddr(address), nil, true) {
			t.Errorf("%s is reachable for an internal rule", address)
		}
	}
}
