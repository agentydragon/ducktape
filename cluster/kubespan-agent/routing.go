package main

import (
	"fmt"
	"io"
	"net"
	"net/netip"
	"os/exec"

	"github.com/vishvananda/netlink"
)

// Routing/firewall constants matching Talos defaults.
// Ref: talos/pkg/machinery/constants/constants.go
const (
	RoutingTable       = 180  // KubeSpanDefaultRoutingTable
	ForceFirewallMark  = 0x40 // KubeSpanDefaultForceFirewallMark (force-route mark)
	FirewallMask       = 0x60 // KubeSpanDefaultFirewallMask
	RulePriority       = 32500
)

// RoutingManager manages nftables rules and ip policy routing for KubeSpan.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (nftables setup)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (RulesManager)
type RoutingManager struct {
	mtu int
}

// NewRoutingManager creates a new routing manager.
func NewRoutingManager(mtu int) *RoutingManager {
	return &RoutingManager{mtu: mtu}
}

// Install sets up nftables rules, ip policy routing rules, and default routes.
//
// nftables chains:
//   - kubespan_prerouting (filter/prerouting): mark incoming packets for peer IPs with 0x40
//   - kubespan_outgoing (route/output): mark outgoing packets for peer IPs with 0x40, MSS clamp
//
// Both chains skip packets already marked with 0x20 (WireGuard encrypted egress).
//
// ip rules:
//   - fwmark 0x40/0x60 → table 180 (priority 32500) for both IPv4 and IPv6
//
// Routes:
//   - Default routes in table 180 via kubespan interface
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go
func (rm *RoutingManager) Install(routedPrefixes []netip.Prefix) error {
	if err := rm.installNftables(routedPrefixes); err != nil {
		return fmt.Errorf("nftables: %w", err)
	}

	if err := rm.installIPRules(); err != nil {
		return fmt.Errorf("ip rules: %w", err)
	}

	if err := rm.installRoutes(); err != nil {
		return fmt.Errorf("routes: %w", err)
	}

	return nil
}

// Update refreshes the nftables rules with the current set of routed prefixes.
func (rm *RoutingManager) Update(routedPrefixes []netip.Prefix) error {
	return rm.installNftables(routedPrefixes)
}

// Cleanup removes all nftables rules, ip rules, and routes installed by kubespand.
func (rm *RoutingManager) Cleanup() error {
	// Delete nftables table (removes all chains and rules).
	_ = exec.Command("nft", "delete", "table", "inet", "talos_kubespan").Run()

	// Delete ip rules.
	rm.deleteIPRules()

	// Routes in table 180 disappear when the kubespan interface is deleted.
	return nil
}

// installNftables creates the talos_kubespan nftables table with two chains.
//
// This uses the nft CLI for simplicity and correctness (the google/nftables Go library
// has incomplete support for sets with intervals and concatenated element types).
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
func (rm *RoutingManager) installNftables(routedPrefixes []netip.Prefix) error {
	// Build the nftables ruleset as an atomic nft -f script.
	var v4Elements, v6Elements string
	for _, p := range routedPrefixes {
		cidr := p.String()
		if p.Addr().Is4() {
			if v4Elements != "" {
				v4Elements += ", "
			}
			v4Elements += cidr
		} else {
			if v6Elements != "" {
				v6Elements += ", "
			}
			v6Elements += cidr
		}
	}

	// We use "flush table" + re-add to atomically update.
	script := "table inet talos_kubespan\ndelete table inet talos_kubespan\n"
	script += "table inet talos_kubespan {\n"

	// IPv4 set of routed prefixes.
	script += "  set routed_v4 {\n"
	script += "    type ipv4_addr\n"
	script += "    flags interval\n"
	if v4Elements != "" {
		script += fmt.Sprintf("    elements = { %s }\n", v4Elements)
	}
	script += "  }\n"

	// IPv6 set of routed prefixes.
	script += "  set routed_v6 {\n"
	script += "    type ipv6_addr\n"
	script += "    flags interval\n"
	if v6Elements != "" {
		script += fmt.Sprintf("    elements = { %s }\n", v6Elements)
	}
	script += "  }\n"

	// Prerouting chain: mark incoming packets destined for routed IPs.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (kubespan_prerouting)
	script += "  chain kubespan_prerouting {\n"
	script += "    type filter hook prerouting priority raw; policy accept;\n"
	// Skip packets already marked by WireGuard (egress encrypted packets).
	script += fmt.Sprintf("    meta mark & 0x%x == 0x%x accept\n", FirewallMask, FirewallMark)
	// Mark packets destined for routed IPv4/IPv6 prefixes.
	script += fmt.Sprintf("    ip daddr @routed_v4 meta mark set meta mark | 0x%x accept\n", ForceFirewallMark)
	script += fmt.Sprintf("    ip6 daddr @routed_v6 meta mark set meta mark | 0x%x accept\n", ForceFirewallMark)
	script += "  }\n"

	// Output chain: mark outgoing packets + MSS clamping.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (kubespan_outgoing)
	script += "  chain kubespan_outgoing {\n"
	script += "    type route hook output priority raw; policy accept;\n"
	// Skip WireGuard egress.
	script += fmt.Sprintf("    meta mark & 0x%x == 0x%x accept\n", FirewallMask, FirewallMark)
	// Skip loopback.
	script += "    oifname \"lo\" accept\n"
	// MSS clamp for routed traffic.
	mss := rm.mtu - 40 // IPv4 header
	if mss > 0 {
		script += fmt.Sprintf("    ip daddr @routed_v4 tcp flags syn / syn,rst tcp option maxseg size set %d\n", mss)
	}
	mss6 := rm.mtu - 60 // IPv6 header
	if mss6 > 0 {
		script += fmt.Sprintf("    ip6 daddr @routed_v6 tcp flags syn / syn,rst tcp option maxseg size set %d\n", mss6)
	}
	// Mark routed packets.
	script += fmt.Sprintf("    ip daddr @routed_v4 meta mark set meta mark | 0x%x accept\n", ForceFirewallMark)
	script += fmt.Sprintf("    ip6 daddr @routed_v6 meta mark set meta mark | 0x%x accept\n", ForceFirewallMark)
	script += "  }\n"

	script += "}\n"

	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = stringReader(script)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft -f: %w\n%s", err, out)
	}

	return nil
}

// installIPRules adds fwmark-based policy routing rules.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (Install)
func (rm *RoutingManager) installIPRules() error {
	for _, family := range []int{netlink.FAMILY_V4, netlink.FAMILY_V6} {
		rule := netlink.NewRule()
		rule.Priority = RulePriority
		rule.Mark = ForceFirewallMark
		rule.Mask = uint32Ptr(FirewallMask)
		rule.Table = RoutingTable
		rule.Family = family

		// Delete existing rule first (idempotent).
		_ = netlink.RuleDel(rule)

		if err := netlink.RuleAdd(rule); err != nil {
			return fmt.Errorf("adding ip rule (family %d): %w", family, err)
		}
	}

	return nil
}

// deleteIPRules removes the fwmark-based policy routing rules.
func (rm *RoutingManager) deleteIPRules() {
	for _, family := range []int{netlink.FAMILY_V4, netlink.FAMILY_V6} {
		rule := netlink.NewRule()
		rule.Priority = RulePriority
		rule.Mark = ForceFirewallMark
		rule.Mask = uint32Ptr(FirewallMask)
		rule.Table = RoutingTable
		rule.Family = family
		_ = netlink.RuleDel(rule)
	}
}

// installRoutes adds default routes in table 180 pointing to the kubespan interface.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (RouteSpec)
func (rm *RoutingManager) installRoutes() error {
	link, err := netlink.LinkByName(LinkName)
	if err != nil {
		return fmt.Errorf("finding %s for routes: %w", LinkName, err)
	}

	// IPv4 default route via kubespan.
	v4Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     RoutingTable,
		Dst:       &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(0, 32)},
		MTU:       rm.mtu,
	}
	if err := netlink.RouteReplace(v4Route); err != nil {
		return fmt.Errorf("adding IPv4 default route to table %d: %w", RoutingTable, err)
	}

	// IPv6 default route via kubespan.
	v6Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     RoutingTable,
		Dst:       &net.IPNet{IP: net.IPv6zero, Mask: net.CIDRMask(0, 128)},
		MTU:       rm.mtu,
	}
	if err := netlink.RouteReplace(v6Route); err != nil {
		return fmt.Errorf("adding IPv6 default route to table %d: %w", RoutingTable, err)
	}

	return nil
}

// stringReader wraps a string as an io.Reader for passing to exec.Command.Stdin.
type stringReaderType struct {
	data []byte
	pos  int
}

func stringReader(s string) *stringReaderType {
	return &stringReaderType{data: []byte(s)}
}

func (r *stringReaderType) Read(p []byte) (n int, err error) {
	if r.pos >= len(r.data) {
		return 0, io.EOF
	}
	n = copy(p, r.data[r.pos:])
	r.pos += n
	return n, nil
}

func uint32Ptr(v uint32) *uint32 {
	return &v
}
