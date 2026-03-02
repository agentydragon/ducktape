// Package routing manages nftables rules and ip policy routing for KubeSpan.
package routing

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"
	"sort"

	"github.com/google/nftables"
	"github.com/google/nftables/binaryutil"
	"github.com/google/nftables/expr"
	"github.com/jsimonetti/rtnetlink/v2"
	"github.com/siderolabs/gen/xslices"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/vishvananda/netlink"
	"go.uber.org/zap"
	"golang.org/x/sys/unix"
)

const tableName = "talos_kubespan"

// RulesManager manages IP policy routing rules for KubeSpan.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go
type RulesManager interface {
	Install() error
	Cleanup() error
}

type rulesManager struct {
	targetTable  uint8
	internalMark uint32
	markMask     uint32
}

// NewRulesManager creates a new IP rules manager matching Talos's routing_rules.go.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (NewRulesManager)
func NewRulesManager(targetTable uint8, internalMark, markMask uint32) RulesManager {
	return &rulesManager{
		targetTable:  targetTable,
		internalMark: internalMark,
		markMask:     markMask,
	}
}

// Install adds fwmark-based policy routing rules for both IPv4 and IPv6.
// Uses jsimonetti/rtnetlink v2 for rule management.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (Install)
func (rm *rulesManager) Install() error {
	nc, err := rtnetlink.Dial(nil)
	if err != nil {
		return fmt.Errorf("rtnetlink dial: %w", err)
	}
	defer nc.Close()

	for _, family := range []uint8{unix.AF_INET, unix.AF_INET6} {
		priority := nextRuleNumber(nc, family)
		table := uint32(rm.targetTable)

		if err := nc.Rule.Replace(&rtnetlink.RuleMessage{
			Family: family,
			Table:  rm.targetTable,
			Action: unix.FR_ACT_TO_TBL,
			Attributes: &rtnetlink.RuleAttributes{
				FwMark:   &rm.internalMark,
				FwMask:   &rm.markMask,
				Table:    &table,
				Priority: &priority,
			},
		}); err != nil {
			return fmt.Errorf("installing ip rule (family %d): %w", family, err)
		}
	}

	return nil
}

// Cleanup removes all fwmark-based policy routing rules matching our mark/mask/table.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (Cleanup)
func (rm *rulesManager) Cleanup() error {
	nc, err := rtnetlink.Dial(nil)
	if err != nil {
		return fmt.Errorf("rtnetlink dial: %w", err)
	}
	defer nc.Close()

	rules, err := nc.Rule.List()
	if err != nil {
		return fmt.Errorf("listing rules: %w", err)
	}

	for _, rule := range rules {
		if rule.Table != rm.targetTable {
			continue
		}
		if rule.Attributes == nil || rule.Attributes.FwMark == nil || rule.Attributes.FwMask == nil {
			continue
		}
		if *rule.Attributes.FwMark != rm.internalMark || *rule.Attributes.FwMask != rm.markMask {
			continue
		}
		if err := nc.Rule.Delete(&rule); err != nil {
			return fmt.Errorf("deleting ip rule: %w", err)
		}
	}

	return nil
}

// nextRuleNumber finds the next available rule priority.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (nextRuleNumber)
func nextRuleNumber(nc *rtnetlink.Conn, family uint8) uint32 {
	rules, err := nc.Rule.List()
	if err != nil {
		return 32500 // fallback
	}

	max := uint32(32499)
	for _, rule := range rules {
		if rule.Family != family {
			continue
		}
		if rule.Attributes != nil && rule.Attributes.Priority != nil {
			if *rule.Attributes.Priority > max && *rule.Attributes.Priority < 32766 {
				max = *rule.Attributes.Priority
			}
		}
	}
	return max + 1
}

// Manager manages nftables rules, ip policy routing rules, and routes for KubeSpan.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (nftables setup)
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go (RulesManager)
type Manager struct {
	mtu          int
	logger       *zap.Logger
	rulesManager RulesManager
}

// NewManager creates a new routing manager.
func NewManager(mtu int, logger *zap.Logger) *Manager {
	return &Manager{
		mtu:    mtu,
		logger: logger,
		rulesManager: NewRulesManager(
			uint8(constants.KubeSpanDefaultRoutingTable),
			constants.KubeSpanDefaultForceFirewallMark,
			constants.KubeSpanDefaultFirewallMask,
		),
	}
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
//   - fwmark 0x40/0x60 → table 180 (dynamic priority) for both IPv4 and IPv6
//
// Routes:
//   - Default routes in table 180 via kubespan interface
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/routing_rules.go
func (rm *Manager) Install(routedPrefixes []netip.Prefix) error {
	// Clean up stale rules from a prior crash before installing new ones.
	if err := rm.Cleanup(); err != nil {
		rm.logger.Warn("pre-install cleanup failed (may be first run)", zap.Error(err))
	}

	if err := rm.installNftables(routedPrefixes); err != nil {
		return fmt.Errorf("nftables: %w", err)
	}

	if err := rm.rulesManager.Install(); err != nil {
		return fmt.Errorf("ip rules: %w", err)
	}

	if err := rm.installRoutes(); err != nil {
		return fmt.Errorf("routes: %w", err)
	}

	return nil
}

// Update refreshes the nftables rules with the current set of routed prefixes.
func (rm *Manager) Update(routedPrefixes []netip.Prefix) error {
	return rm.installNftables(routedPrefixes)
}

// Cleanup removes all nftables rules, ip rules, and routes installed by kubespand.
func (rm *Manager) Cleanup() error {
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables conn: %w", err)
	}

	conn.DelTable(&nftables.Table{
		Family: nftables.TableFamilyINet,
		Name:   tableName,
	})
	_ = conn.Flush() // ignore error if table doesn't exist

	if err := rm.rulesManager.Cleanup(); err != nil {
		rm.logger.Warn("failed to cleanup ip rules", zap.Error(err))
	}

	// Routes in table 180 disappear when the kubespan interface is deleted.
	return nil
}

// installNftables creates the talos_kubespan nftables table with two chains.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
func (rm *Manager) installNftables(routedPrefixes []netip.Prefix) error {
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

	v4Prefixes := xslices.Filter(routedPrefixes, func(p netip.Prefix) bool { return p.Addr().Is4() })
	v6Prefixes := xslices.Filter(routedPrefixes, func(p netip.Prefix) bool { return !p.Addr().Is4() })

	// Share sets between prerouting and output chains (same prefixes).
	v4Set := makeIPv4Set(table, v4Prefixes)
	v6Set := makeIPv6Set(table, v6Prefixes)

	if err := conn.AddSet(v4Set.set, v4Set.elements); err != nil {
		return fmt.Errorf("adding v4 set: %w", err)
	}
	if err := conn.AddSet(v6Set.set, v6Set.elements); err != nil {
		return fmt.Errorf("adding v6 set: %w", err)
	}

	// Prerouting chain: mark incoming packets destined for routed IPs.
	policy := nftables.ChainPolicyAccept
	prerouteChain := conn.AddChain(&nftables.Chain{
		Name:     "kubespan_prerouting",
		Table:    table,
		Type:     nftables.ChainTypeFilter,
		Hooknum:  nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw,
		Policy:   &policy,
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: skipWGMarkExprs(),
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: markIPv4Exprs(v4Set.set),
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: prerouteChain,
		Exprs: markIPv6Exprs(v6Set.set),
	})

	// Output chain: mark outgoing packets + MSS clamping.
	outputChain := conn.AddChain(&nftables.Chain{
		Name:     "kubespan_outgoing",
		Table:    table,
		Type:     nftables.ChainTypeRoute,
		Hooknum:  nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw,
		Policy:   &policy,
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: skipWGMarkExprs(),
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: skipLoopbackExprs(),
	})

	mss4 := rm.mtu - 40
	if mss4 > 0 {
		conn.AddRule(&nftables.Rule{
			Table: table,
			Chain: outputChain,
			Exprs: mssClampIPv4Exprs(v4Set.set, uint16(mss4)),
		})
	}
	mss6 := rm.mtu - 60
	if mss6 > 0 {
		conn.AddRule(&nftables.Rule{
			Table: table,
			Chain: outputChain,
			Exprs: mssClampIPv6Exprs(v6Set.set, uint16(mss6)),
		})
	}

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: markIPv4Exprs(v4Set.set),
	})

	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: outputChain,
		Exprs: markIPv6Exprs(v6Set.set),
	})

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

func makeIPv4Set(table *nftables.Table, prefixes []netip.Prefix) *intervalSet {
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

func makeIPv6Set(table *nftables.Table, prefixes []netip.Prefix) *intervalSet {
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

// prefixesToSetElements converts IP prefixes into nftables interval set elements.
// Ref: talos/internal/app/machined/pkg/adapters/network/nftables_rule.go (SetElements)
func prefixesToSetElements(prefixes []netip.Prefix, addrLen int) []nftables.SetElement {
	if len(prefixes) == 0 {
		return nil
	}

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
		p = p.Masked()
		startBytes := p.Addr().As16()

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

func prefixEnd(p netip.Prefix) netip.Addr {
	addr := p.Addr()
	bits := p.Bits()

	totalBits := 128
	if addr.Is4() {
		totalBits = 32
	}

	if bits == totalBits {
		return incrementAddr(addr)
	}

	if addr.Is4() {
		ip4 := addr.As4()
		for i := bits; i < 32; i++ {
			ip4[i/8] |= 1 << (7 - i%8)
		}
		return incrementAddr(netip.AddrFrom4(ip4))
	}

	b := addr.As16()
	for i := bits; i < 128; i++ {
		b[i/8] |= 1 << (7 - i%8)
	}
	return incrementAddr(netip.AddrFrom16(b))
}

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

func skipWGMarkExprs() []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultFirewallMask),
			Xor:            binaryutil.NativeEndian.PutUint32(0),
		},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultFirewallMark),
		},
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

func skipLoopbackExprs() []expr.Any {
	loName := make([]byte, 16)
	copy(loName, "lo\x00")

	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyOIFNAME, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     loName,
		},
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

func markIPv4Exprs(set *nftables.Set) []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV4},
		},
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       16,
			Len:          4,
		},
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(^uint32(constants.KubeSpanDefaultForceFirewallMark)),
			Xor:            binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultForceFirewallMark),
		},
		&expr.Meta{Key: expr.MetaKeyMARK, SourceRegister: true, Register: 1},
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

func markIPv6Exprs(set *nftables.Set) []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV6},
		},
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       24,
			Len:          16,
		},
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		&expr.Meta{Key: expr.MetaKeyMARK, Register: 1},
		&expr.Bitwise{
			SourceRegister: 1,
			DestRegister:   1,
			Len:            4,
			Mask:           binaryutil.NativeEndian.PutUint32(^uint32(constants.KubeSpanDefaultForceFirewallMark)),
			Xor:            binaryutil.NativeEndian.PutUint32(constants.KubeSpanDefaultForceFirewallMark),
		},
		&expr.Meta{Key: expr.MetaKeyMARK, SourceRegister: true, Register: 1},
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}

func mssClampIPv4Exprs(set *nftables.Set, mss uint16) []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV4},
		},
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       16,
			Len:          4,
		},
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		&expr.Meta{Key: expr.MetaKeyL4PROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.IPPROTO_TCP},
		},
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
		&expr.Exthdr{
			DestRegister: 1,
			Type:         2,
			Offset:       2,
			Len:          2,
			Op:           expr.ExthdrOpTcpopt,
		},
		&expr.Cmp{
			Op:       expr.CmpOpGt,
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		&expr.Immediate{
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		&expr.Exthdr{
			SourceRegister: 1,
			Type:           2,
			Offset:         2,
			Len:            2,
			Op:             expr.ExthdrOpTcpopt,
		},
	}
}

func mssClampIPv6Exprs(set *nftables.Set, mss uint16) []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.NFPROTO_IPV6},
		},
		&expr.Payload{
			DestRegister: 1,
			Base:         expr.PayloadBaseNetworkHeader,
			Offset:       24,
			Len:          16,
		},
		&expr.Lookup{
			SourceRegister: 1,
			SetName:        set.Name,
			SetID:          set.ID,
		},
		&expr.Meta{Key: expr.MetaKeyL4PROTO, Register: 1},
		&expr.Cmp{
			Op:       expr.CmpOpEq,
			Register: 1,
			Data:     []byte{unix.IPPROTO_TCP},
		},
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
		&expr.Exthdr{
			DestRegister: 1,
			Type:         2,
			Offset:       2,
			Len:          2,
			Op:           expr.ExthdrOpTcpopt,
		},
		&expr.Cmp{
			Op:       expr.CmpOpGt,
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		&expr.Immediate{
			Register: 1,
			Data:     binary.BigEndian.AppendUint16(nil, mss),
		},
		&expr.Exthdr{
			SourceRegister: 1,
			Type:           2,
			Offset:         2,
			Len:            2,
			Op:             expr.ExthdrOpTcpopt,
		},
	}
}

// installRoutes adds default routes in table 180 pointing to the kubespan interface.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (RouteSpec)
// TODO: consider aligning nftables with Talos NfTablesChain COSI resources
func (rm *Manager) installRoutes() error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		return fmt.Errorf("finding %s for routes: %w", constants.KubeSpanLinkName, err)
	}

	v4Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(0, 32)},
		MTU:       rm.mtu,
	}
	if err := netlink.RouteReplace(v4Route); err != nil {
		return fmt.Errorf("adding IPv4 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

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
