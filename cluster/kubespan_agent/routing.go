package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"sort"
	"syscall"

	"github.com/google/nftables"
	"github.com/google/nftables/binaryutil"
	"github.com/google/nftables/expr"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/vishvananda/netlink"
	"go.uber.org/zap"
	"golang.org/x/sys/unix"
)

// RulePriority for ip rule entries directing marked traffic to the KubeSpan routing table.
const RulePriority = 32500

const tableName = "talos_kubespan"

// RoutingManager manages nftables rules and ip policy routing for KubeSpan.
// Uses the google/nftables Go library (same as Talos) for atomic nftables management.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (nftables setup)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (RulesManager)
type RoutingManager struct {
	mtu    int
	logger *zap.Logger
}

// NewRoutingManager creates a new routing manager.
func NewRoutingManager(mtu int, logger *zap.Logger) *RoutingManager {
	return &RoutingManager{mtu: mtu, logger: logger}
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
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables conn: %w", err)
	}

	// Delete table (removes all chains, sets, and rules atomically).
	conn.DelTable(&nftables.Table{
		Family: nftables.TableFamilyINet,
		Name:   tableName,
	})
	_ = conn.Flush() // ignore error if table doesn't exist

	rm.deleteIPRules()
	// Routes in table 180 disappear when the kubespan interface is deleted.
	return nil
}

// installNftables creates the talos_kubespan nftables table with two chains.
// Uses the google/nftables Go library with interval sets for prefix matching.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
// Ref: talos/internal/app/machined/pkg/adapters/network/nftables_rule.go
func (rm *RoutingManager) installNftables(routedPrefixes []netip.Prefix) error {
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables conn: %w", err)
	}

	table := &nftables.Table{
		Family: nftables.TableFamilyINet,
		Name:   tableName,
	}

	// Atomically replace: delete existing table, then re-create.
	conn.DelTable(table)
	table = conn.AddTable(table)

	// Separate prefixes by address family.
	var v4Prefixes, v6Prefixes []netip.Prefix
	for _, p := range routedPrefixes {
		if p.Addr().Is4() {
			v4Prefixes = append(v4Prefixes, p)
		} else {
			v6Prefixes = append(v6Prefixes, p)
		}
	}

	// Build anonymous interval sets for IPv4 and IPv6 prefix matching.
	// Ref: talos/internal/app/machined/pkg/adapters/network/nftables_rule.go (SetElements)
	v4PrerouteSet := rm.makeIPv4Set(table, v4Prefixes)
	v6PrerouteSet := rm.makeIPv6Set(table, v6Prefixes)
	v4OutputSet := rm.makeIPv4Set(table, v4Prefixes)
	v6OutputSet := rm.makeIPv6Set(table, v6Prefixes)

	if err := conn.AddSet(v4PrerouteSet.set, v4PrerouteSet.elements); err != nil {
		return fmt.Errorf("adding v4 preroute set: %w", err)
	}
	if err := conn.AddSet(v6PrerouteSet.set, v6PrerouteSet.elements); err != nil {
		return fmt.Errorf("adding v6 preroute set: %w", err)
	}
	if err := conn.AddSet(v4OutputSet.set, v4OutputSet.elements); err != nil {
		return fmt.Errorf("adding v4 output set: %w", err)
	}
	if err := conn.AddSet(v6OutputSet.set, v6OutputSet.elements); err != nil {
		return fmt.Errorf("adding v6 output set: %w", err)
	}

	// Prerouting chain: mark incoming packets destined for routed IPs.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (kubespan_prerouting)
	policy := nftables.ChainPolicyAccept
	prerouteChain := conn.AddChain(&nftables.Chain{
		Name:     "kubespan_prerouting",
		Table:    table,
		Type:     nftables.ChainTypeFilter,
		Hooknum:  nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw,
		Policy:   &policy,
	})

	// Rule: skip packets already marked by WireGuard (egress encrypted packets).
	// meta mark & 0x60 == 0x20 accept
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: rm.skipWGMarkExprs(),
	})

	// Rule: ip daddr @routed_v4 meta mark set meta mark | 0x40 accept
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: rm.markIPv4Exprs(v4PrerouteSet.set),
	})

	// Rule: ip6 daddr @routed_v6 meta mark set meta mark | 0x40 accept
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: rm.markIPv6Exprs(v6PrerouteSet.set),
	})

	// Output chain: mark outgoing packets + MSS clamping.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (kubespan_outgoing)
	outputChain := conn.AddChain(&nftables.Chain{
		Name:     "kubespan_outgoing",
		Table:    table,
		Type:     nftables.ChainTypeRoute,
		Hooknum:  nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw,
		Policy:   &policy,
	})

	// Rule: skip WireGuard egress.
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: rm.skipWGMarkExprs(),
	})

	// Rule: skip loopback.
	// oifname "lo" accept
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: rm.skipLoopbackExprs(),
	})

	// MSS clamping rules for routed traffic.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
	mss4 := rm.mtu - 40 // IPv4 header (20) + TCP header (20)
	if mss4 > 0 {
		conn.AddRule(&nftables.Rule{
			Table: table,
			Chain: outputChain,
			Exprs: rm.mssClampIPv4Exprs(v4OutputSet.set, uint16(mss4)),
		})
	}
	mss6 := rm.mtu - 60 // IPv6 header (40) + TCP header (20)
	if mss6 > 0 {
		conn.AddRule(&nftables.Rule{
			Table: table,
			Chain: outputChain,
			Exprs: rm.mssClampIPv6Exprs(v6OutputSet.set, uint16(mss6)),
		})
	}

	// Rule: mark routed IPv4 packets.
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: rm.markIPv4Exprs(v4OutputSet.set),
	})

	// Rule: mark routed IPv6 packets.
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: rm.markIPv6Exprs(v6OutputSet.set),
	})

	// Flush atomically applies all operations.
	if err := conn.Flush(); err != nil {
		return fmt.Errorf("nftables flush: %w", err)
	}

	return nil
}

// intervalSet holds an nftables set and its pre-computed elements.
type intervalSet struct {
	set      *nftables.Set
	elements []nftables.SetElement
}

// makeIPv4Set creates an anonymous constant interval set for IPv4 prefixes.
func (rm *RoutingManager) makeIPv4Set(table *nftables.Table, prefixes []netip.Prefix) *intervalSet {
	set := &nftables.Set{
		Table:     table,
		Anonymous: true,
		Constant:  true,
		Interval:  true,
		KeyType:   nftables.TypeIPAddr,
	}
	return &intervalSet{
		set:      set,
		elements: prefixesToSetElements(prefixes, 4),
	}
}

// makeIPv6Set creates an anonymous constant interval set for IPv6 prefixes.
func (rm *RoutingManager) makeIPv6Set(table *nftables.Table, prefixes []netip.Prefix) *intervalSet {
	set := &nftables.Set{
		Table:     table,
		Anonymous: true,
		Constant:  true,
		Interval:  true,
		KeyType:   nftables.TypeIP6Addr,
	}
	return &intervalSet{
		set:      set,
		elements: prefixesToSetElements(prefixes, 16),
	}
}

// prefixesToSetElements converts a list of IP prefixes into nftables interval set elements.
// Each prefix becomes two elements: [network_addr, IntervalEnd=false] and [end_addr, IntervalEnd=true].
// Ref: talos/internal/app/machined/pkg/adapters/network/nftables_rule.go (SetElements)
func prefixesToSetElements(prefixes []netip.Prefix, addrLen int) []nftables.SetElement {
	if len(prefixes) == 0 {
		return nil
	}

	// Sort prefixes for deterministic set construction.
	sorted := make([]netip.Prefix, len(prefixes))
	copy(sorted, prefixes)
	sort.Slice(sorted, func(i, j int) bool {
		ai, aj := sorted[i].Addr(), sorted[j].Addr()
		if c := ai.Compare(aj); c != 0 {
			return c < 0
		}
		return sorted[i].Bits() < sorted[j].Bits()
	})

	var elements []nftables.SetElement
	for _, p := range sorted {
		p = p.Masked() // Normalize to network address.
		startBytes := p.Addr().As16()

		// Compute the end address (first address past the prefix range).
		endAddr := prefixEnd(p)
		endBytes := endAddr.As16()

		var start, end []byte
		if addrLen == 4 {
			start = startBytes[12:16]
			end = endBytes[12:16]
		} else {
			start = startBytes[:]
			end = endBytes[:]
		}

		elements = append(elements,
			nftables.SetElement{Key: start, IntervalEnd: false},
			nftables.SetElement{Key: end, IntervalEnd: true},
		)
	}

	return elements
}

// prefixEnd returns the first address past the end of a prefix range.
// For example, 10.0.0.0/24 → 10.0.1.0.
func prefixEnd(p netip.Prefix) netip.Addr {
	addr := p.Addr()
	bits := p.Bits()

	b := addr.As16()

	// Total bits for this address family.
	totalBits := 128
	if addr.Is4() {
		totalBits = 32
	}

	// For a /totalBits prefix (single address), the end is addr+1.
	// For shorter prefixes, we compute the network + size of the prefix block.
	if bits == totalBits {
		return incrementAddr(addr)
	}

	// Set the host part to all-ones, then increment.
	// This gives us the first address past the prefix range.
	if addr.Is4() {
		ip4 := addr.As4()
		maskLen := bits
		for i := maskLen; i < 32; i++ {
			ip4[i/8] |= 1 << (7 - i%8)
		}
		a := netip.AddrFrom4(ip4)
		return incrementAddr(a)
	}

	for i := bits; i < 128; i++ {
		b[i/8] |= 1 << (7 - i%8)
	}
	a := netip.AddrFrom16(b)
	return incrementAddr(a)
}

// incrementAddr adds 1 to an IP address.
func incrementAddr(addr netip.Addr) netip.Addr {
	b := addr.As16()
	for i := len(b) - 1; i >= 0; i-- {
		b[i]++
		if b[i] != 0 {
			break
		}
	}
	if addr.Is4() {
		return netip.AddrFrom4([4]byte{b[12], b[13], b[14], b[15]})
	}
	return netip.AddrFrom16(b)
}

// skipWGMarkExprs returns expressions for: meta mark & 0x60 == 0x20 accept
// Skips packets already marked by WireGuard (egress encrypted packets).
func (rm *RoutingManager) skipWGMarkExprs() []expr.Any {
	return []expr.Any{
		// Load meta mark → reg 1
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		// Bitwise: reg1 = reg1 & constants.KubeSpanDefaultFirewallMask
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultFirewallMask),
			Xor:            binaryutil.NativeEndian.PutUint32(0),
		},
		// Compare: reg1 == KubeSpanDefaultFirewallMark (0x20, WG egress mark)
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultFirewallMark),
		},
		// Verdict: accept
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

// skipLoopbackExprs returns expressions for: oifname "lo" accept
func (rm *RoutingManager) skipLoopbackExprs() []expr.Any {
	// oifname is a 16-byte field (IFNAMSIZ).
	loName := make([]byte, 16)
	copy(loName, "lo\x00")

	return []expr.Any{
		// Load output interface name → reg 1
		&expr.Meta{Key: expr.MetaKeyOIFNAME, Register: 1},
		// Compare: reg1 == "lo"
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     loName,
		},
		// Verdict: accept
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

// markIPv4Exprs returns expressions for: ip daddr @set meta mark set meta mark | 0x40 accept
func (rm *RoutingManager) markIPv4Exprs(set *nftables.Set) []expr.Any {
	return []expr.Any{
		// Check nfproto == IPv4
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV4},
		},
		// Load IPv4 destination address → reg 1 (offset 16 in network header, 4 bytes)
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       16,
			Len:          4,
		},
		// Lookup in set
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		// Load current mark → reg 1
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		// OR with constants.KubeSpanDefaultForceFirewallMark: reg1 = (reg1 & 0xffffffff) ^ constants.KubeSpanDefaultForceFirewallMark
		// Actually: mark | 0x40 = (mark & ~0x40) ^ 0x40 ... but simpler: bitwise OR.
		// Bitwise OR: reg1 = (reg1 & 0xffffffff) | 0x40
		// nftables bitwise: result = (sreg & mask) ^ xor
		// To compute OR with X: mask = 0xffffffff, xor = X → result = (reg & 0xff..) ^ X
		// But that's XOR not OR. For OR: mask = ~X, xor = X → result = (reg & ~X) | X
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(^uint32(constants.KubeSpanDefaultForceFirewallMark)),
			Xor:            binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultForceFirewallMark),
		},
		// Set mark from reg 1
		&expr.Meta{Key: expr.MetaKeyMARK, SourceRegister: true, Register: 1},
		// Verdict: accept
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

// markIPv6Exprs returns expressions for: ip6 daddr @set meta mark set meta mark | 0x40 accept
func (rm *RoutingManager) markIPv6Exprs(set *nftables.Set) []expr.Any {
	return []expr.Any{
		// Check nfproto == IPv6
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV6},
		},
		// Load IPv6 destination address → reg 1 (offset 24 in network header, 16 bytes)
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       24,
			Len:          16,
		},
		// Lookup in set
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		// Load current mark
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		// OR with constants.KubeSpanDefaultForceFirewallMark
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(^uint32(constants.KubeSpanDefaultForceFirewallMark)),
			Xor:            binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultForceFirewallMark),
		},
		// Set mark
		&expr.Meta{Key: expr.MetaKeyMARK, SourceRegister: true, Register: 1},
		// Accept
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

// mssClampIPv4Exprs returns expressions for TCP MSS clamping on IPv4 routed traffic.
// ip daddr @set tcp flags syn / syn,rst tcp option maxseg size set <mss>
func (rm *RoutingManager) mssClampIPv4Exprs(set *nftables.Set, mss uint16) []expr.Any {
	return []expr.Any{
		// Check nfproto == IPv4
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV4},
		},
		// Load IPv4 daddr → reg 1
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       16,
			Len:          4,
		},
		// Lookup in set
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		// Check L4 protocol == TCP
		&expr.Meta{Key: expr.MetaKeyL4PROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.IPPROTO_TCP},
		},
		// Check TCP flags: SYN set, RST not set → (flags & (SYN|RST)) == SYN
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseTransportHeader,
			Offset:       13, // TCP flags byte offset
			Len:          1,
		},
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            1,
			Mask:           []byte{0x06}, // SYN (0x02) | RST (0x04)
			Xor:            []byte{0x00},
		},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{0x02}, // SYN only
		},
		// Read current MSS option → reg 1
		&expr.Exthdr{
			DestRegister: 1,
			Type:         2, // TCP MSS option kind
			Offset:       2, // MSS value offset within the option
			Len:          2,
			Op:           expr.ExthdrOpTcpopt,
		},
		// Compare: MSS > target → clamp
		&expr.Cmp{
			Op:       expr.CmpOpGt,
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		// Load target MSS value → reg 1
		&expr.Immediate{
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		// Write MSS option
		&expr.Exthdr{
			SourceRegister: 1,
			Type:           2,
			Offset:         2,
			Len:            2,
			Op:             expr.ExthdrOpTcpopt,
		},
	}
}

// mssClampIPv6Exprs returns expressions for TCP MSS clamping on IPv6 routed traffic.
func (rm *RoutingManager) mssClampIPv6Exprs(set *nftables.Set, mss uint16) []expr.Any {
	return []expr.Any{
		// Check nfproto == IPv6
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV6},
		},
		// Load IPv6 daddr → reg 1
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       24,
			Len:          16,
		},
		// Lookup in set
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		// Check L4 proto == TCP
		&expr.Meta{Key: expr.MetaKeyL4PROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.IPPROTO_TCP},
		},
		// Check TCP SYN without RST
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseTransportHeader,
			Offset:       13,
			Len:          1,
		},
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            1,
			Mask:           []byte{0x06},
			Xor:            []byte{0x00},
		},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{0x02},
		},
		// Read current MSS option
		&expr.Exthdr{
			DestRegister: 1,
			Type:         2,
			Offset:       2,
			Len:          2,
			Op:           expr.ExthdrOpTcpopt,
		},
		// Compare: MSS > target
		&expr.Cmp{
			Op:       expr.CmpOpGt,
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		// Load target MSS
		&expr.Immediate{
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		// Write MSS option
		&expr.Exthdr{
			SourceRegister: 1,
			Type:           2,
			Offset:         2,
			Len:            2,
			Op:             expr.ExthdrOpTcpopt,
		},
	}
}

// makeIPRule builds the fwmark-based policy routing rule for a given address family.
func makeIPRule(family int) *netlink.Rule {
	rule := netlink.NewRule()
	rule.Priority = RulePriority
	rule.Mark = constants.KubeSpanDefaultForceFirewallMark
	rule.Mask = uint32Ptr(constants.KubeSpanDefaultFirewallMask)
	rule.Table = constants.KubeSpanDefaultRoutingTable
	rule.Family = family
	return rule
}

// installIPRules adds fwmark-based policy routing rules.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (Install)
func (rm *RoutingManager) installIPRules() error {
	for _, family := range []int{netlink.FAMILY_V4, netlink.FAMILY_V6} {
		rule := makeIPRule(family)

		// Delete existing rule first (idempotent). ESRCH means rule
		// doesn't exist, which is expected on first install.
		if err := netlink.RuleDel(rule); err != nil && !errors.Is(err, syscall.ESRCH) {
			rm.logger.Warn("failed to delete old ip rule", zap.Int("family", family), zap.Error(err))
		}

		if err := netlink.RuleAdd(rule); err != nil {
			return fmt.Errorf("adding ip rule (family %d): %w", family, err)
		}
	}

	return nil
}

// deleteIPRules removes the fwmark-based policy routing rules.
func (rm *RoutingManager) deleteIPRules() {
	for _, family := range []int{netlink.FAMILY_V4, netlink.FAMILY_V6} {
		if err := netlink.RuleDel(makeIPRule(family)); err != nil && !errors.Is(err, syscall.ESRCH) {
			rm.logger.Warn("failed to delete ip rule", zap.Int("family", family), zap.Error(err))
		}
	}
}

// installRoutes adds default routes in table 180 pointing to the kubespan interface.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (RouteSpec)
func (rm *RoutingManager) installRoutes() error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		return fmt.Errorf("finding %s for routes: %w", constants.KubeSpanLinkName, err)
	}

	// IPv4 default route via kubespan.
	v4Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(0, 32)},
		MTU:       rm.mtu,
	}
	if err := netlink.RouteReplace(v4Route); err != nil {
		return fmt.Errorf("adding IPv4 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	// IPv6 default route via kubespan.
	v6Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv6zero, Mask: net.CIDRMask(0, 128)},
		MTU:       rm.mtu,
	}
	if err := netlink.RouteReplace(v6Route); err != nil {
		return fmt.Errorf("adding IPv6 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	return nil
}

func uint32Ptr(v uint32) *uint32 {
	return &v
}
