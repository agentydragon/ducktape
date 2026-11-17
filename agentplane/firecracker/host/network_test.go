package host

import (
	"context"
	"errors"
	"fmt"
	"net"
	"slices"
	"testing"

	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"
)

type fakeNetworkOperations struct {
	links     map[string]netlink.Link
	addresses map[string][]netlink.Addr
	firewalls map[string]*fakeFirewall
	ipForward bool
	fail      func(family, operation, table, chain string, rulespec []string) error
}

func newFakeNetworkOperations() *fakeNetworkOperations {
	ops := &fakeNetworkOperations{
		links:     make(map[string]netlink.Link),
		addresses: make(map[string][]netlink.Addr),
		firewalls: make(map[string]*fakeFirewall),
	}
	for _, family := range []string{"IPv4", "IPv6"} {
		ops.firewalls[family] = newFakeFirewall(family, ops)
	}
	return ops
}

func (f *fakeNetworkOperations) LinkList() ([]netlink.Link, error) {
	if err := f.failOperation("", "link-list", "", "", nil); err != nil {
		return nil, err
	}
	links := make([]netlink.Link, 0, len(f.links))
	for _, link := range f.links {
		links = append(links, link)
	}
	return links, nil
}

func (f *fakeNetworkOperations) LinkByName(name string) (netlink.Link, error) {
	if err := f.failOperation("", "link-by-name", "", name, nil); err != nil {
		return nil, err
	}
	link, ok := f.links[name]
	if !ok {
		return nil, fmt.Errorf("link %q does not exist", name)
	}
	return link, nil
}

func (f *fakeNetworkOperations) LinkAdd(link netlink.Link) error {
	if err := f.failOperation("", "link-add", "", link.Attrs().Name, nil); err != nil {
		return err
	}
	if _, exists := f.addresses[link.Attrs().Name]; !exists {
		f.addresses[link.Attrs().Name] = nil
	}
	f.links[link.Attrs().Name] = link
	return nil
}

func (f *fakeNetworkOperations) LinkDel(link netlink.Link) error {
	if err := f.failOperation("", "link-del", "", link.Attrs().Name, nil); err != nil {
		return err
	}
	delete(f.links, link.Attrs().Name)
	delete(f.addresses, link.Attrs().Name)
	return nil
}

func (f *fakeNetworkOperations) AddrAdd(link netlink.Link, addr *netlink.Addr) error {
	if err := f.failOperation("", "address-add", "", link.Attrs().Name, nil); err != nil {
		return err
	}
	copy := *addr
	f.addresses[link.Attrs().Name] = append(f.addresses[link.Attrs().Name], copy)
	return nil
}

func (f *fakeNetworkOperations) LinkSetMTU(link netlink.Link, mtu int) error {
	if err := f.failOperation("", "link-mtu", "", link.Attrs().Name, nil); err != nil {
		return err
	}
	link.Attrs().MTU = mtu
	return nil
}

func (f *fakeNetworkOperations) LinkSetUp(link netlink.Link) error {
	if err := f.failOperation("", "link-up", "", link.Attrs().Name, nil); err != nil {
		return err
	}
	link.Attrs().Flags |= net.FlagUp
	return nil
}

func (f *fakeNetworkOperations) IPv4() firewallOperations { return f.firewalls["IPv4"] }
func (f *fakeNetworkOperations) IPv6() firewallOperations { return f.firewalls["IPv6"] }

func (f *fakeNetworkOperations) EnableIPv4Forwarding() error {
	if err := f.failOperation("", "enable-forwarding", "", "", nil); err != nil {
		return err
	}
	f.ipForward = true
	return nil
}

func (f *fakeNetworkOperations) failOperation(family, operation, table, chain string, rulespec []string) error {
	if f.fail == nil {
		return nil
	}
	return f.fail(family, operation, table, chain, rulespec)
}

type fakeFirewall struct {
	family string
	owner  *fakeNetworkOperations
	chains map[string][][]string
	order  []string
}

func newFakeFirewall(family string, owner *fakeNetworkOperations) *fakeFirewall {
	return &fakeFirewall{
		family: family,
		owner:  owner,
		chains: map[string][][]string{
			"filter/INPUT":   {},
			"filter/FORWARD": {},
			"nat/PREROUTING": {},
		},
	}
}

func (f *fakeFirewall) key(table, chain string) string { return table + "/" + chain }

func (f *fakeFirewall) before(operation, table, chain string, rulespec []string) error {
	return f.owner.failOperation(f.family, operation, table, chain, rulespec)
}

func (f *fakeFirewall) ChainExists(table, chain string) (bool, error) {
	if err := f.before("chain-exists", table, chain, nil); err != nil {
		return false, err
	}
	_, exists := f.chains[f.key(table, chain)]
	return exists, nil
}

func (f *fakeFirewall) NewChain(table, chain string) error {
	if err := f.before("new-chain", table, chain, nil); err != nil {
		return err
	}
	key := f.key(table, chain)
	f.chains[key] = nil
	f.order = append(f.order, key)
	return nil
}

func (f *fakeFirewall) DeleteChain(table, chain string) error {
	if err := f.before("delete-chain", table, chain, nil); err != nil {
		return err
	}
	key := f.key(table, chain)
	delete(f.chains, key)
	for index := len(f.order) - 1; index >= 0; index-- {
		if f.order[index] == key {
			f.order = slices.Delete(f.order, index, index+1)
		}
	}
	return nil
}

func (f *fakeFirewall) ClearChain(table, chain string) error {
	if err := f.before("clear-chain", table, chain, nil); err != nil {
		return err
	}
	key := f.key(table, chain)
	if _, exists := f.chains[key]; !exists {
		return fmt.Errorf("chain %s does not exist", key)
	}
	f.chains[key] = nil
	return nil
}

func (f *fakeFirewall) Exists(table, chain string, rulespec ...string) (bool, error) {
	if err := f.before("exists", table, chain, rulespec); err != nil {
		return false, err
	}
	key := f.key(table, chain)
	if _, exists := f.chains[key]; !exists {
		return false, fmt.Errorf("chain %s does not exist", key)
	}
	for _, rule := range f.chains[key] {
		if slices.Equal(rule, rulespec) {
			return true, nil
		}
	}
	return false, nil
}

func (f *fakeFirewall) Append(table, chain string, rulespec ...string) error {
	if err := f.before("append", table, chain, rulespec); err != nil {
		return err
	}
	key := f.key(table, chain)
	if _, exists := f.chains[key]; !exists {
		return fmt.Errorf("chain %s does not exist", key)
	}
	f.chains[key] = append(f.chains[key], slices.Clone(rulespec))
	return nil
}

func (f *fakeFirewall) Insert(table, chain string, pos int, rulespec ...string) error {
	if err := f.before("insert", table, chain, rulespec); err != nil {
		return err
	}
	key := f.key(table, chain)
	if _, exists := f.chains[key]; !exists {
		return fmt.Errorf("chain %s does not exist", key)
	}
	rules := f.chains[key]
	index := max(pos-1, 0)
	if index > len(rules) {
		index = len(rules)
	}
	rules = append(rules, nil)
	copy(rules[index+1:], rules[index:])
	rules[index] = slices.Clone(rulespec)
	f.chains[key] = rules
	return nil
}

func (f *fakeFirewall) Delete(table, chain string, rulespec ...string) error {
	if err := f.before("delete", table, chain, rulespec); err != nil {
		return err
	}
	key := f.key(table, chain)
	rules, exists := f.chains[key]
	if !exists {
		return fmt.Errorf("chain %s does not exist", key)
	}
	for index, rule := range rules {
		if slices.Equal(rule, rulespec) {
			f.chains[key] = slices.Delete(rules, index, index+1)
			return nil
		}
	}
	return fmt.Errorf("rule not found in %s", key)
}

func (f *fakeFirewall) List(table, chain string) ([]string, error) {
	if err := f.before("list", table, chain, nil); err != nil {
		return nil, err
	}
	rules, exists := f.chains[f.key(table, chain)]
	if !exists {
		return nil, fmt.Errorf("chain %s does not exist", f.key(table, chain))
	}
	listed := make([]string, len(rules))
	for index, rule := range rules {
		listed[index] = fmt.Sprint(rule)
	}
	return listed, nil
}

func (f *fakeFirewall) rules(table, chain string) [][]string {
	rules := f.chains[f.key(table, chain)]
	result := make([][]string, len(rules))
	for index, rule := range rules {
		result[index] = slices.Clone(rule)
	}
	return result
}

func (f *fakeFirewall) hasRule(table, chain string, expected ...string) bool {
	for _, rule := range f.chains[f.key(table, chain)] {
		if slices.Equal(rule, expected) {
			return true
		}
	}
	return false
}

func TestNetworkSetupIsIdempotentAndTeardownRemovesManagedState(t *testing.T) {
	ops := newFakeNetworkOperations()
	network := newNetwork(3128, []int{8443}, ops)
	ctx := context.Background()

	if err := network.Setup(ctx); err != nil {
		t.Fatal(err)
	}
	assertTapConfigured(t, ops)
	assertSetupFirewall(t, ops)
	assertGuardsAbsent(t, ops)

	chainsBefore := map[string][]string{}
	parentRulesBefore := map[string]map[string][][]string{}
	for family, fw := range ops.firewalls {
		chainsBefore[family] = slices.Clone(fw.order)
		parentRulesBefore[family] = map[string][][]string{
			"filter/INPUT":   fw.rules("filter", "INPUT"),
			"filter/FORWARD": fw.rules("filter", "FORWARD"),
			"nat/PREROUTING": fw.rules("nat", "PREROUTING"),
		}
	}
	if err := network.Setup(ctx); err != nil {
		t.Fatal(err)
	}
	assertTapConfigured(t, ops)
	assertGuardsAbsent(t, ops)
	for family, fw := range ops.firewalls {
		if !slices.Equal(fw.order, chainsBefore[family]) {
			t.Errorf("%s managed chain order after setup rerun = %v, want %v", family, fw.order, chainsBefore[family])
		}
		for key, want := range parentRulesBefore[family] {
			got := fw.chains[key]
			if !equalRules(got, want) {
				t.Errorf("%s parent rules %s after setup rerun = %v, want %v", family, key, got, want)
			}
		}
	}
	assertSetupFirewall(t, ops)

	if err := network.Enable(ctx); err != nil {
		t.Fatal(err)
	}
	assertEnabledFirewall(t, ops)
	assertGuardsAbsent(t, ops)

	if err := network.Disable(ctx); err != nil {
		t.Fatal(err)
	}
	assertSetupFirewall(t, ops)
	assertGuardsAbsent(t, ops)

	if err := network.Teardown(ctx); err != nil {
		t.Fatal(err)
	}
	assertNetworkRemoved(t, ops)
	assertGuardsAbsent(t, ops)
	if err := network.Teardown(ctx); err != nil {
		t.Fatalf("second teardown: %v", err)
	}
	assertNetworkRemoved(t, ops)
	assertGuardsAbsent(t, ops)
}

func assertTapConfigured(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	link, exists := ops.links[Tap]
	if !exists {
		t.Fatal("TAP link is absent")
	}
	tap, ok := link.(*netlink.Tuntap)
	if !ok {
		t.Fatalf("link type = %T, want Tuntap", link)
	}
	if tap.Mode != netlink.TUNTAP_MODE_TAP || tap.Flags&netlink.TUNTAP_NO_PI == 0 {
		t.Errorf("TAP mode/flags = %d/%d, want TAP with NO_PI", tap.Mode, tap.Flags)
	}
	if link.Attrs().MTU != 1280 || link.Attrs().Flags&net.FlagUp == 0 {
		t.Errorf("TAP MTU/flags = %d/%v, want 1280/up", link.Attrs().MTU, link.Attrs().Flags)
	}
	addresses := ops.addresses[Tap]
	if len(addresses) != 2 || addresses[0].IPNet.String() != gatewayV4+"/30" || addresses[1].IPNet.String() != gatewayV6+"/64" || addresses[1].Flags&unix.IFA_F_NODAD == 0 {
		t.Errorf("TAP addresses = %v, want IPv4 then IPv6 with NODAD", addresses)
	}
	if !ops.ipForward {
		t.Error("IPv4 forwarding is disabled")
	}
}

func assertSetupFirewall(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	wantChains := []string{"filter/" + inputChain, "filter/" + forwardChain, "filter/" + ingressChain}
	for family, fw := range ops.firewalls {
		if !slices.Equal(fw.order, wantChains) {
			t.Errorf("%s chain creation order = %v, want %v", family, fw.order, wantChains)
		}
		if family == "IPv6" {
			assertRules(t, family, fw, "filter", inputChain, [][]string{
				{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-solicitation", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
				{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-advertisement", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
				{"-j", "REJECT"},
			})
			assertRules(t, family, fw, "filter", forwardChain, [][]string{{"-j", "REJECT"}})
			assertRules(t, family, fw, "filter", ingressChain, [][]string{{"-j", "REJECT"}})
		} else {
			assertRules(t, family, fw, "filter", inputChain, [][]string{{"-j", "REJECT"}})
			assertRules(t, family, fw, "filter", forwardChain, [][]string{{"-j", "REJECT"}})
			assertRules(t, family, fw, "filter", ingressChain, [][]string{{"-j", "REJECT"}})
		}
		assertRules(t, family, fw, "filter", "INPUT", [][]string{{"-i", Tap, "-j", inputChain}})
		assertRules(t, family, fw, "filter", "FORWARD", [][]string{
			{"-i", Tap, "-j", forwardChain},
			{"-o", Tap, "-j", ingressChain},
		})
	}
	assertRules(t, "IPv4", ops.firewalls["IPv4"], "nat", "PREROUTING", [][]string{{"-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", GuestV4 + ":9090"}})
	assertRules(t, "IPv6", ops.firewalls["IPv6"], "nat", "PREROUTING", nil)
}

func assertEnabledFirewall(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	for family, fw := range ops.firewalls {
		input := [][]string{{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"}}
		if family == "IPv6" {
			input = append(
				input,
				[]string{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-solicitation", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
				[]string{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-advertisement", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
			)
			// Neighbor discovery remains ahead of host reply and port allows.
			input = append([][]string{input[1], input[2], input[0]}, input[3:]...)
		}
		input = append(
			input,
			[]string{"-p", "tcp", "--dport", "3128", "-j", "ACCEPT"},
			[]string{"-p", "tcp", "--dport", "8443", "-j", "ACCEPT"},
			[]string{"-j", "REJECT"},
		)
		assertRules(t, family, fw, "filter", inputChain, input)
		assertRules(t, family, fw, "filter", forwardChain, [][]string{
			{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"},
			{"-j", "REJECT"},
		})
		ingress := [][]string{}
		if family == "IPv4" {
			ingress = append(ingress, []string{"-d", GuestV4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT"})
		}
		ingress = append(
			ingress,
			[]string{"-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"},
			[]string{"-j", "REJECT"},
		)
		assertRules(t, family, fw, "filter", ingressChain, ingress)
	}
}

func assertNetworkRemoved(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	if _, exists := ops.links[Tap]; exists {
		t.Error("TAP link remains after teardown")
	}
	for family, fw := range ops.firewalls {
		for _, chain := range managedChains {
			if _, exists := fw.chains[fw.key("filter", chain)]; exists {
				t.Errorf("%s chain %s remains after teardown", family, chain)
			}
		}
		for _, parent := range []string{"INPUT", "FORWARD"} {
			for _, rule := range fw.rules("filter", parent) {
				if slices.Contains(rule, inputChain) || slices.Contains(rule, forwardChain) || slices.Contains(rule, ingressChain) {
					t.Errorf("%s %s retains managed jump %v", family, parent, rule)
				}
			}
		}
		if family == "IPv4" && len(fw.rules("nat", "PREROUTING")) != 0 {
			t.Errorf("IPv4 nat PREROUTING retains managed rule: %v", fw.rules("nat", "PREROUTING"))
		}
	}
}

func assertGuardsAbsent(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	for family, fw := range ops.firewalls {
		for _, parent := range []string{"INPUT", "FORWARD"} {
			for _, rule := range fw.rules("filter", parent) {
				if len(rule) >= 4 && slices.Equal(rule[:2], []string{"-i", Tap}) && rule[2] == "-j" && rule[3] == "REJECT" {
					t.Errorf("%s guard remains in %s: %v", family, parent, rule)
				}
				if len(rule) >= 4 && slices.Equal(rule[:2], []string{"-o", Tap}) && rule[2] == "-j" && rule[3] == "REJECT" {
					t.Errorf("%s guard remains in %s: %v", family, parent, rule)
				}
			}
		}
	}
}

func assertGuardsPresent(t *testing.T, ops *fakeNetworkOperations) {
	t.Helper()
	for family, fw := range ops.firewalls {
		if !fw.hasRule("filter", "INPUT", "-i", Tap, "-j", "REJECT") ||
			!fw.hasRule("filter", "FORWARD", "-i", Tap, "-j", "REJECT") ||
			!fw.hasRule("filter", "FORWARD", "-o", Tap, "-j", "REJECT") {
			t.Errorf("%s does not retain every fail-closed guard", family)
		}
	}
}

func assertRules(t *testing.T, family string, fw *fakeFirewall, table, chain string, want [][]string) {
	t.Helper()
	got := fw.rules(table, chain)
	if !equalRules(got, want) {
		t.Errorf("%s %s/%s rules = %v, want %v", family, table, chain, got, want)
	}
}

func equalRules(a, b [][]string) bool {
	if len(a) != len(b) {
		return false
	}
	for index := range a {
		if !slices.Equal(a[index], b[index]) {
			return false
		}
	}
	return true
}

func TestNetworkSetupFailureKeepsTrafficFenced(t *testing.T) {
	ops := newFakeNetworkOperations()
	failure := errors.New("link creation failed")
	ops.fail = func(_ string, operation, _, _ string, _ []string) error {
		if operation == "link-add" {
			return failure
		}
		return nil
	}
	if err := newNetwork(3128, nil, ops).Setup(context.Background()); !errors.Is(err, failure) {
		t.Fatalf("Setup error = %v, want link creation failure", err)
	}
	assertGuardsPresent(t, ops)
	if len(ops.links) != 0 {
		t.Errorf("link creation failure left links: %v", ops.links)
	}
}

func TestNetworkRuleFailureKeepsTrafficFenced(t *testing.T) {
	ops := newFakeNetworkOperations()
	network := newNetwork(3128, []int{8443}, ops)
	if err := network.Setup(context.Background()); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("policy update failed")
	ops.fail = func(family, operation, table, chain string, rulespec []string) error {
		if family == "IPv4" && operation == "append" && table == "filter" && chain == inputChain && slices.Equal(rulespec, []string{"-p", "tcp", "--dport", "3128", "-j", "ACCEPT"}) {
			return failure
		}
		return nil
	}
	if err := network.Enable(context.Background()); !errors.Is(err, failure) {
		t.Fatalf("Enable error = %v, want policy update failure", err)
	}
	assertGuardsPresent(t, ops)
}

func TestNetworkTeardownFailureKeepsTrafficFenced(t *testing.T) {
	ops := newFakeNetworkOperations()
	network := newNetwork(3128, nil, ops)
	if err := network.Setup(context.Background()); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("chain removal failed")
	ops.fail = func(family, operation, table, chain string, _ []string) error {
		if family == "IPv6" && operation == "delete-chain" && table == "filter" && chain == ingressChain {
			return failure
		}
		return nil
	}
	if err := network.Teardown(context.Background()); !errors.Is(err, failure) {
		t.Fatalf("Teardown error = %v, want chain removal failure", err)
	}
	assertGuardsPresent(t, ops)
	if _, exists := ops.links[Tap]; !exists {
		t.Error("failed teardown removed the TAP link while firewall cleanup was incomplete")
	}
}
