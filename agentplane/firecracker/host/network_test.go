package host

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
)

type firewallCall struct {
	family string
	method string
	table  string
	chain  string
	rule   []string
}

type recordingFirewall struct {
	family string
	calls  *[]firewallCall
	guards map[string]int
	fail   func(firewallCall) error
}

func newRecordingNetwork(relayPort int, platformPorts []int) (*Network, *recordingFirewall, *recordingFirewall, *[]firewallCall) {
	calls := new([]firewallCall)
	guards4, guards6 := make(map[string]int), make(map[string]int)
	ipv4 := &recordingFirewall{family: "IPv4", calls: calls, guards: guards4}
	ipv6 := &recordingFirewall{family: "IPv6", calls: calls, guards: guards6}
	return newNetwork(relayPort, platformPorts, ipv4, ipv6), ipv4, ipv6, calls
}

func (f *recordingFirewall) record(method, table, chain string, rule ...string) error {
	call := firewallCall{family: f.family, method: method, table: table, chain: chain, rule: slices.Clone(rule)}
	*f.calls = append(*f.calls, call)
	if f.fail != nil {
		return f.fail(call)
	}
	return nil
}

func (f *recordingFirewall) guardKey(table, chain string, rule []string) string {
	return table + "/" + chain + "/" + strings.Join(rule, "\x00")
}

func (f *recordingFirewall) isGuard(table, chain string, rule []string) bool {
	for _, guard := range firewallGuards {
		if guard.family == f.family && guard.parent == chain && slices.Equal(guard.rule, rule) && table == "filter" {
			return true
		}
	}
	return false
}

func (f *recordingFirewall) ChainExists(table, chain string) (bool, error) {
	return false, f.record("chain-exists", table, chain)
}

func (f *recordingFirewall) NewChain(table, chain string) error {
	return f.record("new-chain", table, chain)
}

func (f *recordingFirewall) ClearChain(table, chain string) error {
	return f.record("clear-chain", table, chain)
}

func (f *recordingFirewall) Exists(table, chain string, rule ...string) (bool, error) {
	if err := f.record("exists", table, chain, rule...); err != nil {
		return false, err
	}
	return f.guards[f.guardKey(table, chain, rule)] > 0, nil
}

func (f *recordingFirewall) Append(table, chain string, rule ...string) error {
	return f.record("append", table, chain, rule...)
}

func (f *recordingFirewall) AppendUnique(table, chain string, rule ...string) error {
	return f.record("append-unique", table, chain, rule...)
}

func (f *recordingFirewall) Insert(table, chain string, pos int, rule ...string) error {
	if err := f.record("insert", table, chain, rule...); err != nil {
		return err
	}
	if f.isGuard(table, chain, rule) {
		f.guards[f.guardKey(table, chain, rule)]++
	}
	return nil
}

func (f *recordingFirewall) Delete(table, chain string, rule ...string) error {
	if err := f.record("delete", table, chain, rule...); err != nil {
		return err
	}
	if f.isGuard(table, chain, rule) {
		f.guards[f.guardKey(table, chain, rule)]--
	}
	return nil
}

func TestNetworkEnableKeepsGuardsUntilBothFamiliesAreRebuilt(t *testing.T) {
	network, _, _, calls := newRecordingNetwork(3128, []int{8443})
	if err := network.Enable(context.Background()); err != nil {
		t.Fatal(err)
	}

	firstClear, lastAppend, firstDelete := -1, -1, -1
	for i, call := range *calls {
		if call.method == "clear-chain" && firstClear < 0 {
			firstClear = i
		}
		if call.method == "append" {
			lastAppend = i
		}
		if call.method == "delete" && firstDelete < 0 {
			firstDelete = i
		}
	}
	if firstClear < len(firewallGuards) || lastAppend < 0 || (*calls)[lastAppend].family != "IPv6" || firstDelete <= lastAppend {
		t.Fatalf("guard/update order = %#v; want both families rebuilt before guard removal", *calls)
	}
	for _, family := range []string{"IPv4", "IPv6"} {
		for _, chain := range managedChains {
			cleared := false
			for _, call := range (*calls)[:firstDelete] {
				if call.family == family && call.method == "clear-chain" && call.chain == chain {
					cleared = true
					break
				}
			}
			if !cleared {
				t.Errorf("%s chain %s was not rebuilt before guards were removed", family, chain)
			}
		}
	}
}

func TestNetworkUpdateFailureLeavesGuardsInstalled(t *testing.T) {
	network, ipv4, ipv6, calls := newRecordingNetwork(3128, []int{8443})
	failure := errors.New("iptables rejected an update")
	ipv4.fail = func(call firewallCall) error {
		if call.method == "append" && call.chain == inputChain && slices.Equal(call.rule, []string{"-p", "tcp", "--dport", "3128", "-j", "ACCEPT"}) {
			return failure
		}
		return nil
	}
	if err := network.Enable(context.Background()); !errors.Is(err, failure) {
		t.Fatalf("Enable error = %v, want update failure", err)
	}
	for _, fw := range []*recordingFirewall{ipv4, ipv6} {
		for _, guard := range firewallGuards {
			if guard.family == fw.family && fw.guards[fw.guardKey("filter", guard.parent, guard.rule)] != 1 {
				t.Errorf("%s guard %s %v is not retained", fw.family, guard.parent, guard.rule)
			}
		}
	}
	for _, call := range *calls {
		if call.method == "delete" {
			t.Fatalf("failed update removed a guard: %#v", call)
		}
	}
}

func TestNetworkDisableLeavesRejectPolicyAndIPv6NeighborDiscovery(t *testing.T) {
	network, _, _, calls := newRecordingNetwork(3128, []int{8443})
	if err := network.Disable(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := map[string][][]string{}
	for _, call := range *calls {
		if call.method == "append" {
			key := call.family + "/" + call.chain
			got[key] = append(got[key], call.rule)
		}
	}
	want := map[string][][]string{
		"IPv4/" + inputChain:   {{"-j", "REJECT"}},
		"IPv4/" + forwardChain: {{"-j", "REJECT"}},
		"IPv4/" + ingressChain: {{"-j", "REJECT"}},
		"IPv6/" + inputChain: {
			{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-solicitation", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
			{"-p", "ipv6-icmp", "--icmpv6-type", "neighbor-advertisement", "-m", "hl", "--hl-eq", "255", "-j", "ACCEPT"},
			{"-j", "REJECT"},
		},
		"IPv6/" + forwardChain: {{"-j", "REJECT"}},
		"IPv6/" + ingressChain: {{"-j", "REJECT"}},
	}
	if len(got) != len(want) {
		t.Fatalf("disabled policy chains = %v, want %v", got, want)
	}
	for key, rules := range want {
		if !equalRules(got[key], rules) {
			t.Errorf("%s rules = %v, want %v", key, got[key], rules)
		}
	}
}

func equalRules(got, want [][]string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if !slices.Equal(got[i], want[i]) {
			return false
		}
	}
	return true
}
