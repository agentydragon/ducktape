package upstream

import (
	"net/netip"
	"slices"

	"code.dny.dev/ssrf"
)

// guard carries the IANA special-purpose registries: everything outside the IPv6 global unicast
// range, and every registered range inside it or in IPv4, is refused. Its ranges are the
// registries', wider than a dialer needs, so it also refuses shared address space and the
// anycast ranges (AS112, AMT) and NAT64 (`64:ff9b::/96`, which can wrap any IPv4 address,
// loopback included).
var guard = ssrf.New()

// Reachable reports whether the proxy may dial address: a globally reachable unicast address,
// one inside an exempt network, or -- when the deciding rule declared its host cluster-internal --
// a private one (RFC 1918 or ULA), which excludes loopback and link-local: those are the Pod's own
// interfaces and the node's metadata service, not the cluster network a rule means.
//
// An IPv4-mapped IPv6 address is judged as the IPv4 address it wraps, so `::ffff:10.0.0.1` is as
// private as `10.0.0.1` and an exemption of `127.0.0.0/8` covers `::ffff:127.0.0.1`.
func Reachable(address netip.Addr, exempt []netip.Prefix, internal bool) bool {
	// A zone is no part of the address, and `netip.Prefix.Contains` refuses a zoned one.
	address = address.WithZone("").Unmap()
	switch {
	case !address.IsValid():
		return false
	case slices.ContainsFunc(exempt, func(prefix netip.Prefix) bool { return prefix.Contains(address) }):
		return true
	case internal && address.IsPrivate():
		return true
	default:
		return guard.SafeAddr(address) == nil
	}
}
