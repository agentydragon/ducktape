// Package routing manages ip policy routing rules and routes for KubeSpan.
// Nftables management has moved to the nftables package (NfTablesChainController).
package routing

import (
	"fmt"
	"net"

	"github.com/jsimonetti/rtnetlink/v2"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

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

// InstallRoutes adds default routes in table 180 pointing to the kubespan interface.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (RouteSpec)
func InstallRoutes(mtu int) error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		return fmt.Errorf("finding %s for routes: %w", constants.KubeSpanLinkName, err)
	}

	v4Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(0, 32)},
		MTU:       mtu,
	}
	if err := netlink.RouteReplace(v4Route); err != nil {
		return fmt.Errorf("adding IPv4 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	v6Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv6zero, Mask: net.CIDRMask(0, 128)},
		MTU:       mtu,
	}
	if err := netlink.RouteReplace(v6Route); err != nil {
		return fmt.Errorf("adding IPv6 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	return nil
}
