// Kubespand-specific: endpoint filtering for the PeerSpec controller.
// Talos does not filter endpoints; kubespand supports configurable CIDR-based
// allow/deny filters via ConfigSpec.EndpointFilters.
package kubespan

import (
	"net/netip"
	"slices"
	"strings"
)

// endpointFilter is a parsed CIDR filter with allow/deny semantics.
type endpointFilter struct {
	prefix netip.Prefix
	deny   bool
}

// parseEndpointFilters parses endpoint filter strings ("!cidr" for deny, "cidr" for allow).
func parseEndpointFilters(raw []string) []endpointFilter {
	var filters []endpointFilter
	for _, s := range raw {
		deny := false
		cidr := s
		if strings.HasPrefix(s, "!") {
			deny = true
			cidr = s[1:]
		}
		prefix, err := netip.ParsePrefix(cidr)
		if err != nil {
			continue
		}
		filters = append(filters, endpointFilter{prefix: prefix, deny: deny})
	}
	return filters
}

// filterEndpoints returns only the endpoints allowed by the filter list.
// When no filters are configured, all endpoints are returned (matching upstream behavior).
func filterEndpoints(endpoints []netip.AddrPort, filters []endpointFilter) []netip.AddrPort {
	if len(filters) == 0 {
		return slices.Clone(endpoints)
	}
	var result []netip.AddrPort
	for _, ep := range endpoints {
		if endpointAllowed(ep, filters) {
			result = append(result, ep)
		}
	}
	return result
}

// endpointAllowed checks if an endpoint is allowed by the filter list.
// First match wins. Empty filters = allow all.
func endpointAllowed(ep netip.AddrPort, filters []endpointFilter) bool {
	if len(filters) == 0 {
		return true
	}
	for _, f := range filters {
		if f.prefix.Contains(ep.Addr()) {
			return !f.deny
		}
	}
	return false
}
