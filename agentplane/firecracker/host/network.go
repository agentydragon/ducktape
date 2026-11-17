package host

import (
	"context"
	"errors"
	"fmt"
	"net"
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

type firewallOperations interface {
	ChainExists(table, chain string) (bool, error)
	NewChain(table, chain string) error
	DeleteChain(table, chain string) error
	ClearChain(table, chain string) error
	Exists(table, chain string, rulespec ...string) (bool, error)
	Append(table, chain string, rulespec ...string) error
	Insert(table, chain string, pos int, rulespec ...string) error
	Delete(table, chain string, rulespec ...string) error
	List(table, chain string) ([]string, error)
}

// networkOperations is the seam between the policy we own and the host
// networking libraries. Tests fake these operations and inspect resulting
// links and rules rather than shell command strings.
type networkOperations interface {
	LinkList() ([]netlink.Link, error)
	LinkByName(name string) (netlink.Link, error)
	LinkAdd(link netlink.Link) error
	LinkDel(link netlink.Link) error
	AddrAdd(link netlink.Link, addr *netlink.Addr) error
	LinkSetMTU(link netlink.Link, mtu int) error
	LinkSetUp(link netlink.Link) error
	IPv4() firewallOperations
	IPv6() firewallOperations
	EnableIPv4Forwarding() error
}

type hostNetworkOperations struct {
	ipv4 firewallOperations
	ipv6 firewallOperations
}

func newHostNetworkOperations() (*hostNetworkOperations, error) {
	ipv4, err := iptables.NewWithProtocol(iptables.ProtocolIPv4)
	if err != nil {
		return nil, fmt.Errorf("create IPv4 iptables client: %w", err)
	}
	ipv6, err := iptables.NewWithProtocol(iptables.ProtocolIPv6)
	if err != nil {
		return nil, fmt.Errorf("create IPv6 iptables client: %w", err)
	}
	return &hostNetworkOperations{ipv4: ipv4, ipv6: ipv6}, nil
}

func (hostNetworkOperations) LinkList() ([]netlink.Link, error) { return netlink.LinkList() }
func (hostNetworkOperations) LinkByName(name string) (netlink.Link, error) {
	return netlink.LinkByName(name)
}
func (hostNetworkOperations) LinkAdd(link netlink.Link) error { return netlink.LinkAdd(link) }
func (hostNetworkOperations) LinkDel(link netlink.Link) error { return netlink.LinkDel(link) }
func (hostNetworkOperations) AddrAdd(link netlink.Link, addr *netlink.Addr) error {
	return netlink.AddrAdd(link, addr)
}

func (hostNetworkOperations) LinkSetMTU(link netlink.Link, mtu int) error {
	return netlink.LinkSetMTU(link, mtu)
}
func (hostNetworkOperations) LinkSetUp(link netlink.Link) error { return netlink.LinkSetUp(link) }
func (ops hostNetworkOperations) IPv4() firewallOperations      { return ops.ipv4 }
func (ops hostNetworkOperations) IPv6() firewallOperations      { return ops.ipv6 }
func (hostNetworkOperations) EnableIPv4Forwarding() error {
	if err := os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644); err != nil {
		return fmt.Errorf("enable IPv4 forwarding: %w", err)
	}
	return nil
}

type Network struct {
	relayPort     int
	platformPorts []int
	ops           networkOperations
	initErr       error
}

func NewNetwork(relayPort int, platformPorts []int) *Network {
	ops, err := newHostNetworkOperations()
	return &Network{
		relayPort:     relayPort,
		platformPorts: append([]int(nil), platformPorts...),
		ops:           ops,
		initErr:       err,
	}
}

func newNetwork(relayPort int, platformPorts []int, ops networkOperations) *Network {
	return &Network{
		relayPort:     relayPort,
		platformPorts: append([]int(nil), platformPorts...),
		ops:           ops,
	}
}

type networkFamily struct {
	name     string
	iptables firewallOperations
}

func (n *Network) families() []networkFamily {
	return []networkFamily{{name: "IPv4", iptables: n.ops.IPv4()}, {name: "IPv6", iptables: n.ops.IPv6()}}
}

func (n *Network) checkReady(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if n.initErr != nil {
		return n.initErr
	}
	if n.ops == nil || n.ops.IPv4() == nil || n.ops.IPv6() == nil {
		return errors.New("host network operations are unavailable")
	}
	return nil
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
	if err := n.checkReady(ctx); err != nil {
		return err
	}
	return n.withGuards(ctx, func() error {
		links, err := n.ops.LinkList()
		if err != nil {
			return fmt.Errorf("list host links: %w", err)
		}
		for _, link := range links {
			if link.Attrs().Name == Tap {
				if err := n.ops.LinkDel(link); err != nil {
					return fmt.Errorf("remove existing %s link: %w", Tap, err)
				}
				break
			}
		}

		tap := &netlink.Tuntap{
			LinkAttrs: netlink.LinkAttrs{Name: Tap},
			Mode:      netlink.TUNTAP_MODE_TAP,
			Flags:     netlink.TUNTAP_NO_PI,
		}
		if err := n.ops.LinkAdd(tap); err != nil {
			return fmt.Errorf("create %s TAP link: %w", Tap, err)
		}
		link, err := n.ops.LinkByName(Tap)
		if err != nil {
			return fmt.Errorf("look up %s link: %w", Tap, err)
		}
		for _, item := range []struct {
			cidr  string
			flags int
		}{
			{cidr: gatewayV4 + "/30"},
			{cidr: gatewayV6 + "/64", flags: unix.IFA_F_NODAD},
		} {
			addr, err := parseNetworkAddress(item.cidr, item.flags)
			if err != nil {
				return err
			}
			if err := n.ops.AddrAdd(link, addr); err != nil {
				return fmt.Errorf("assign %s to %s: %w", item.cidr, Tap, err)
			}
		}
		if err := n.ops.LinkSetMTU(link, 1280); err != nil {
			return fmt.Errorf("set %s MTU: %w", Tap, err)
		}
		if err := n.ops.LinkSetUp(link); err != nil {
			return fmt.Errorf("bring up %s: %w", Tap, err)
		}
		if err := n.ops.EnableIPv4Forwarding(); err != nil {
			return err
		}
		if err := n.ensureFirewall(ctx); err != nil {
			return err
		}
		return n.writeRules(ctx, false)
	})
}

func parseNetworkAddress(cidr string, flags int) (*netlink.Addr, error) {
	ip, subnet, err := net.ParseCIDR(cidr)
	if err != nil {
		return nil, fmt.Errorf("parse host address %q: %w", cidr, err)
	}
	subnet.IP = ip
	return &netlink.Addr{IPNet: subnet, Flags: flags}, nil
}

func (n *Network) Enable(ctx context.Context) error {
	if err := n.checkReady(ctx); err != nil {
		return err
	}
	return n.withGuards(ctx, func() error { return n.writeRules(ctx, true) })
}

func (n *Network) Disable(ctx context.Context) error {
	if err := n.checkReady(ctx); err != nil {
		return err
	}
	return n.withGuards(ctx, func() error { return n.writeRules(ctx, false) })
}

func (n *Network) Teardown(ctx context.Context) error {
	if err := n.checkReady(ctx); err != nil {
		return err
	}
	return n.withGuards(ctx, func() error {
		if err := removeRule(ctx, n.ops.IPv4(), "nat", "PREROUTING", []string{"-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", GuestV4 + ":9090"}); err != nil {
			return fmt.Errorf("remove Runtime port forwarding: %w", err)
		}
		for _, family := range n.families() {
			for _, jump := range firewallJumps {
				if err := removeRule(ctx, family.iptables, "filter", jump.parent, jump.rule); err != nil {
					return fmt.Errorf("remove %s %s jump: %w", family.name, jump.parent, err)
				}
			}
		}
		families := n.families()
		for familyIndex := len(families) - 1; familyIndex >= 0; familyIndex-- {
			family := families[familyIndex]
			for chainIndex := len(managedChains) - 1; chainIndex >= 0; chainIndex-- {
				chain := managedChains[chainIndex]
				exists, err := family.iptables.ChainExists("filter", chain)
				if err != nil {
					return fmt.Errorf("check %s chain %s: %w", family.name, chain, err)
				}
				if !exists {
					continue
				}
				if err := family.iptables.ClearChain("filter", chain); err != nil {
					return fmt.Errorf("clear %s chain %s: %w", family.name, chain, err)
				}
				rules, err := family.iptables.List("filter", chain)
				if err != nil {
					return fmt.Errorf("list %s chain %s after clearing: %w", family.name, chain, err)
				}
				if len(rules) != 0 {
					return fmt.Errorf("%s chain %s still contains rules after clearing", family.name, chain)
				}
				if err := family.iptables.DeleteChain("filter", chain); err != nil {
					return fmt.Errorf("delete %s chain %s: %w", family.name, chain, err)
				}
			}
		}
		links, err := n.ops.LinkList()
		if err != nil {
			return fmt.Errorf("list host links: %w", err)
		}
		for _, link := range links {
			if link.Attrs().Name == Tap {
				if err := n.ops.LinkDel(link); err != nil {
					return fmt.Errorf("remove %s link: %w", Tap, err)
				}
				break
			}
		}
		return nil
	})
}

func (n *Network) withGuards(ctx context.Context, update func() error) error {
	if err := n.installGuards(ctx); err != nil {
		return err
	}
	if err := update(); err != nil {
		return err
	}
	if err := n.removeGuards(ctx); err != nil {
		return errors.Join(err, n.installGuards(ctx))
	}
	return nil
}

func (n *Network) installGuards(ctx context.Context) error {
	var errs []error
	for _, guard := range firewallGuards {
		if err := ctx.Err(); err != nil {
			return errors.Join(errors.Join(errs...), err)
		}
		fw := n.firewall(guard.family)
		if err := fw.Insert("filter", guard.parent, 1, guard.rule...); err != nil {
			errs = append(errs, fmt.Errorf("install %s %s guard: %w", guard.family, guard.parent, err))
		}
	}
	return errors.Join(errs...)
}

func (n *Network) removeGuards(ctx context.Context) error {
	for _, guard := range firewallGuards {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := removeRule(ctx, n.firewall(guard.family), "filter", guard.parent, guard.rule); err != nil {
			return fmt.Errorf("remove %s %s guard: %w", guard.family, guard.parent, err)
		}
	}
	return nil
}

func removeRule(ctx context.Context, fw firewallOperations, table, chain string, rule []string) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		exists, err := fw.Exists(table, chain, rule...)
		if err != nil {
			return err
		}
		if !exists {
			return nil
		}
		if err := fw.Delete(table, chain, rule...); err != nil {
			return err
		}
	}
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

func (n *Network) ensureFirewall(ctx context.Context) error {
	for _, family := range n.families() {
		for _, chain := range managedChains {
			if err := ctx.Err(); err != nil {
				return err
			}
			exists, err := family.iptables.ChainExists("filter", chain)
			if err != nil {
				return fmt.Errorf("check %s chain %s: %w", family.name, chain, err)
			}
			if !exists {
				if err := family.iptables.NewChain("filter", chain); err != nil {
					return fmt.Errorf("create %s chain %s: %w", family.name, chain, err)
				}
			}
		}
		for _, jump := range firewallJumps {
			if err := ctx.Err(); err != nil {
				return err
			}
			exists, err := family.iptables.Exists("filter", jump.parent, jump.rule...)
			if err != nil {
				return fmt.Errorf("check %s jump to %s: %w", family.name, jump.chain, err)
			}
			if !exists {
				if err := family.iptables.Append("filter", jump.parent, jump.rule...); err != nil {
					return fmt.Errorf("add %s jump to %s: %w", family.name, jump.chain, err)
				}
			}
		}
	}
	natRule := []string{"-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", GuestV4 + ":9090"}
	if err := ctx.Err(); err != nil {
		return err
	}
	exists, err := n.ops.IPv4().Exists("nat", "PREROUTING", natRule...)
	if err != nil {
		return fmt.Errorf("check Runtime port forwarding: %w", err)
	}
	if !exists {
		if err := n.ops.IPv4().Append("nat", "PREROUTING", natRule...); err != nil {
			return fmt.Errorf("add Runtime port forwarding: %w", err)
		}
	}
	return nil
}

func (n *Network) writeRules(ctx context.Context, enabled bool) error {
	for _, family := range n.families() {
		for _, chain := range managedChains {
			if err := ctx.Err(); err != nil {
				return err
			}
			if err := family.iptables.ClearChain("filter", chain); err != nil {
				return fmt.Errorf("clear %s chain %s: %w", family.name, chain, err)
			}
		}
		for _, rule := range n.policyRules(family.name == "IPv6", enabled) {
			if err := ctx.Err(); err != nil {
				return err
			}
			if err := family.iptables.Append("filter", rule.chain, rule.args...); err != nil {
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

func (n *Network) firewall(family string) firewallOperations {
	if family == "IPv6" {
		return n.ops.IPv6()
	}
	return n.ops.IPv4()
}
