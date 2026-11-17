package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"

	"github.com/coreos/go-iptables/iptables"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

const (
	GuestV4   = "192.0.2.2"
	Tap       = "tap0"
	gatewayV4 = "192.0.2.1"
	gatewayV6 = "fd00:fc::1"
)

const (
	inputChain   = "AP_TAP_INPUT"
	forwardChain = "AP_TAP_FORWARD"
	ingressChain = "AP_TAP_INGRESS"
)

var managedChains = []string{inputChain, forwardChain, ingressChain}

type firewall interface {
	ChainExists(table, chain string) (bool, error)
	NewChain(table, chain string) error
	ClearChain(table, chain string) error
	Exists(table, chain string, rulespec ...string) (bool, error)
	Append(table, chain string, rulespec ...string) error
	AppendUnique(table, chain string, rulespec ...string) error
	Insert(table, chain string, pos int, rulespec ...string) error
	Delete(table, chain string, rulespec ...string) error
}

var _ firewall = (*iptables.IPTables)(nil)

type Network struct {
	relayPort     int
	platformPorts []int
	ipv4          firewall
	ipv6          firewall
}

func NewNetwork(relayPort int, platformPorts []int) (*Network, error) {
	ipv4, err := iptables.NewWithProtocol(iptables.ProtocolIPv4)
	if err != nil {
		return nil, fmt.Errorf("create IPv4 iptables client: %w", err)
	}
	ipv6, err := iptables.NewWithProtocol(iptables.ProtocolIPv6)
	if err != nil {
		return nil, fmt.Errorf("create IPv6 iptables client: %w", err)
	}
	return newNetwork(relayPort, platformPorts, ipv4, ipv6), nil
}

func newNetwork(relayPort int, platformPorts []int, ipv4, ipv6 firewall) *Network {
	return &Network{
		relayPort:     relayPort,
		platformPorts: append([]int(nil), platformPorts...),
		ipv4:          ipv4,
		ipv6:          ipv6,
	}
}

type networkFamily struct {
	name string
	fw   firewall
}

func (n *Network) families() []networkFamily {
	return []networkFamily{{name: "IPv4", fw: n.ipv4}, {name: "IPv6", fw: n.ipv6}}
}

type firewallGuard struct {
	family string
	parent string
	rule   []string
}

var firewallGuards = []firewallGuard{
	{family: "IPv4", parent: "INPUT", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{family: "IPv4", parent: "FORWARD", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{family: "IPv4", parent: "FORWARD", rule: []string{"-o", Tap, "-j", "REJECT"}},
	{family: "IPv6", parent: "INPUT", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{family: "IPv6", parent: "FORWARD", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{family: "IPv6", parent: "FORWARD", rule: []string{"-o", Tap, "-j", "REJECT"}},
}

func (n *Network) Setup(ctx context.Context) error {
	return n.withGuards(ctx, func() error {
		link, err := netlink.LinkByName(Tap)
		if err == nil {
			if err := netlink.LinkDel(link); err != nil {
				return fmt.Errorf("remove existing %s link: %w", Tap, err)
			}
		} else {
			var notFound netlink.LinkNotFoundError
			if !errors.As(err, &notFound) {
				return fmt.Errorf("look up existing %s link: %w", Tap, err)
			}
		}

		tap := &netlink.Tuntap{
			LinkAttrs: netlink.LinkAttrs{Name: Tap},
			Mode:      netlink.TUNTAP_MODE_TAP,
			Flags:     netlink.TUNTAP_NO_PI,
		}
		if err := netlink.LinkAdd(tap); err != nil {
			return fmt.Errorf("create %s TAP link: %w", Tap, err)
		}
		link, err = netlink.LinkByName(Tap)
		if err != nil {
			return fmt.Errorf("look up %s link: %w", Tap, err)
		}
		for _, item := range []struct {
			cidr  string
			nodad bool
		}{
			{cidr: gatewayV4 + "/30"},
			{cidr: gatewayV6 + "/64", nodad: true},
		} {
			addr, err := netlink.ParseAddr(item.cidr)
			if err != nil {
				return fmt.Errorf("parse host address %q: %w", item.cidr, err)
			}
			if item.nodad {
				addr.Flags |= unix.IFA_F_NODAD
			}
			if err := netlink.AddrAdd(link, addr); err != nil {
				return fmt.Errorf("assign %s to %s: %w", item.cidr, Tap, err)
			}
		}
		if err := netlink.LinkSetMTU(link, 1280); err != nil {
			return fmt.Errorf("set %s MTU: %w", Tap, err)
		}
		if err := netlink.LinkSetUp(link); err != nil {
			return fmt.Errorf("bring up %s: %w", Tap, err)
		}
		if err := os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644); err != nil {
			return fmt.Errorf("enable IPv4 forwarding: %w", err)
		}
		if err := n.ensureFirewall(); err != nil {
			return err
		}
		return n.writeRules(false)
	})
}

func (n *Network) Enable(ctx context.Context) error {
	return n.withGuards(ctx, func() error { return n.writeRules(true) })
}

func (n *Network) Disable(ctx context.Context) error {
	return n.withGuards(ctx, func() error { return n.writeRules(false) })
}

func (n *Network) withGuards(_ context.Context, update func() error) error {
	if err := n.installGuards(); err != nil {
		return err
	}
	if err := update(); err != nil {
		return err
	}
	if err := n.removeGuards(); err != nil {
		return errors.Join(err, n.installGuards())
	}
	return nil
}

func (n *Network) installGuards() error {
	var errs []error
	for _, guard := range firewallGuards {
		if err := n.firewall(guard.family).Insert("filter", guard.parent, 1, guard.rule...); err != nil {
			errs = append(errs, fmt.Errorf("install %s %s guard: %w", guard.family, guard.parent, err))
		}
	}
	return errors.Join(errs...)
}

func (n *Network) removeGuards() error {
	for _, guard := range firewallGuards {
		fw := n.firewall(guard.family)
		for {
			exists, err := fw.Exists("filter", guard.parent, guard.rule...)
			if err != nil {
				return err
			}
			if !exists {
				break
			}
			if err := fw.Delete("filter", guard.parent, guard.rule...); err != nil {
				return err
			}
		}
	}
	return nil
}

func (n *Network) firewall(family string) firewall {
	if family == "IPv6" {
		return n.ipv6
	}
	return n.ipv4
}

type firewallJump struct {
	chain  string
	parent string
	rule   []string
}

var firewallJumps = []firewallJump{
	{chain: inputChain, parent: "INPUT", rule: []string{"-i", Tap, "-j", inputChain}},
	{chain: forwardChain, parent: "FORWARD", rule: []string{"-i", Tap, "-j", forwardChain}},
	{chain: ingressChain, parent: "FORWARD", rule: []string{"-o", Tap, "-j", ingressChain}},
}

func (n *Network) ensureFirewall() error {
	for _, family := range n.families() {
		for _, chain := range managedChains {
			exists, err := family.fw.ChainExists("filter", chain)
			if err != nil {
				return fmt.Errorf("check %s chain %s: %w", family.name, chain, err)
			}
			if !exists {
				if err := family.fw.NewChain("filter", chain); err != nil {
					return fmt.Errorf("create %s chain %s: %w", family.name, chain, err)
				}
			}
		}
		for _, jump := range firewallJumps {
			if err := family.fw.AppendUnique("filter", jump.parent, jump.rule...); err != nil {
				return fmt.Errorf("add %s jump to %s: %w", family.name, jump.chain, err)
			}
		}
	}
	natRule := []string{"-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", GuestV4 + ":9090"}
	if err := n.ipv4.AppendUnique("nat", "PREROUTING", natRule...); err != nil {
		return fmt.Errorf("add Runtime port forwarding: %w", err)
	}
	return nil
}

func (n *Network) writeRules(enabled bool) error {
	for _, family := range n.families() {
		for _, chain := range managedChains {
			if err := family.fw.ClearChain("filter", chain); err != nil {
				return fmt.Errorf("clear %s chain %s: %w", family.name, chain, err)
			}
		}
		for _, rule := range n.policyRules(family.name == "IPv6", enabled) {
			if err := family.fw.Append("filter", rule.chain, rule.args...); err != nil {
				return fmt.Errorf("append rule to %s chain %s: %w", family.name, rule.chain, err)
			}
		}
	}
	return nil
}

type policyRule struct {
	chain string
	args  []string
}

func (n *Network) policyRules(ipv6, enabled bool) []policyRule {
	rules := make([]policyRule, 0, 12+len(n.platformPorts))
	if ipv6 {
		for _, kind := range []string{"neighbor-solicitation", "neighbor-advertisement"} {
			rules = append(rules, policyRule{chain: inputChain, args: []string{"-p", "ipv6-icmp", "--icmpv6-type", kind, "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"}})
		}
	}
	if enabled {
		rules = append(rules, policyRule{chain: inputChain, args: []string{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"}})
		for _, port := range append([]int{n.relayPort}, n.platformPorts...) {
			rules = append(rules, policyRule{chain: inputChain, args: []string{"-p", "tcp", "--dport", strconv.Itoa(port), "-j", "ACCEPT"}})
		}
	}
	rules = append(rules, policyRule{chain: inputChain, args: []string{"-j", "REJECT"}})
	if enabled {
		rules = append(rules, policyRule{chain: forwardChain, args: []string{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"}})
	}
	rules = append(rules, policyRule{chain: forwardChain, args: []string{"-j", "REJECT"}})
	if enabled && !ipv6 {
		rules = append(rules, policyRule{chain: ingressChain, args: []string{"-d", GuestV4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT"}})
	}
	if enabled {
		rules = append(rules, policyRule{chain: ingressChain, args: []string{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"}})
	}
	rules = append(rules, policyRule{chain: ingressChain, args: []string{"-j", "REJECT"}})
	return rules
}
