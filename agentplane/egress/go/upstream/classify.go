package upstream

import (
	"net/netip"
	"slices"
)

// The special-purpose ranges the IANA registries list as not globally reachable, with their
// globally reachable exceptions, exactly as CPython's `ipaddress` carries them: the proxy's
// verdicts equal `ipaddress.is_global` and `is_private`, which `classify_test.go` pins against
// CPython's own answers. `netip.Addr.IsGlobalUnicast` and `IsPrivate` are not this registry.
var (
	notGlobal4 = mustPrefixes(
		"0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
		"192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
		"203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32",
	)
	global4Exceptions = mustPrefixes("192.0.0.9/32", "192.0.0.10/32")
	notGlobal6        = mustPrefixes(
		"::1/128", "::/128", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16",
		"3fff::/20", "fc00::/7", "fe80::/10",
	)
	global6Exceptions = mustPrefixes(
		"2001:1::1/128", "2001:1::2/128", "2001:3::/32", "2001:4:112::/48", "2001:20::/28", "2001:30::/28",
	)
	// Shared address space (RFC 6598): neither global nor private, which CPython answers false to
	// both. A cluster's own Pod and Service ranges are often carved from it, so it is not
	// `ClusterInternal` and stays unreachable unless exempt.
	sharedAddressSpace = netip.MustParsePrefix("100.64.0.0/10")
)

func mustPrefixes(prefixes ...string) []netip.Prefix {
	parsed := make([]netip.Prefix, len(prefixes))
	for i, prefix := range prefixes {
		parsed[i] = netip.MustParsePrefix(prefix)
	}
	return parsed
}

func containedIn(prefixes []netip.Prefix, address netip.Addr) bool {
	return slices.ContainsFunc(prefixes, func(prefix netip.Prefix) bool { return prefix.Contains(address) })
}

// canonical is the address as the registries judge it: an IPv4-mapped IPv6 address as the IPv4
// address it wraps (so the tables hold no `::ffff:0:0/96` entry), and without the zone
// `netip.Prefix.Contains` would refuse it for.
func canonical(address netip.Addr) netip.Addr {
	return address.WithZone("").Unmap()
}

// private reports a range the registries list as not globally reachable, minus the exceptions.
func private(address netip.Addr) bool {
	if address.Is4() {
		return containedIn(notGlobal4, address) && !containedIn(global4Exceptions, address)
	}
	return address.Is6() && containedIn(notGlobal6, address) && !containedIn(global6Exceptions, address)
}

func global(address netip.Addr) bool {
	return address.IsValid() && !sharedAddressSpace.Contains(address) && !private(address)
}

// ClusterInternal reports a private unicast address a rule declaring its host `clusterInternal`
// may reach: in the registries' not-globally-reachable ranges, other than loopback, link-local,
// multicast and unspecified. A rule declaring its host internal means the cluster network, and the
// Pod's own interfaces are not that: `127.0.0.1` is the sidecar and the runner's own listeners,
// and `169.254.169.254` is whatever the node's metadata service is.
//
// The ranges are the registries' wide set, not only RFC 1918 and ULA: documentation, benchmark and
// reserved space count too, and shared address space (`100.64.0.0/10`) does not.
func ClusterInternal(address netip.Addr) bool {
	address = canonical(address)
	return private(address) &&
		!(address.IsLoopback() || address.IsLinkLocalUnicast() || address.IsMulticast() || address.IsUnspecified())
}

// Reachable reports whether the proxy may dial address: globally reachable unicast by the IANA
// special-purpose registries, inside an exempt network, or -- when the deciding rule declared its
// host cluster-internal -- a private unicast address.
//
// An IPv4-mapped IPv6 address is judged as the IPv4 address it wraps, so `::ffff:10.0.0.1` is as
// private as `10.0.0.1` and an exemption of `127.0.0.0/8` covers `::ffff:127.0.0.1`.
func Reachable(address netip.Addr, exempt []netip.Prefix, internal bool) bool {
	address = canonical(address)
	switch {
	case !address.IsValid():
		return false
	case containedIn(exempt, address):
		return true
	case internal && ClusterInternal(address):
		return true
	default:
		return global(address) && !address.IsMulticast()
	}
}
