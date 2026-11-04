package firecracker

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
)

type systemCall struct {
	command string
	args    []string
}

type fakeSystem struct {
	calls       []systemCall
	checks      []systemCall
	guardCounts map[string]int
	writes      []systemCall
	fail        func(command string, args []string) error
	tapExists   bool
}

func newFakeSystem() *fakeSystem {
	return &fakeSystem{guardCounts: make(map[string]int)}
}

func (f *fakeSystem) Run(_ context.Context, command string, args ...string) error {
	call := systemCall{command: command, args: slices.Clone(args)}
	f.calls = append(f.calls, call)
	if f.fail != nil {
		if err := f.fail(command, args); err != nil {
			return err
		}
	}
	if len(args) > 0 && args[0] == "-I" && len(args) >= 4 {
		f.guardCounts[guardKey(command, args[1], args[3:])]++
	}
	if len(args) > 0 && args[0] == "-D" && len(args) >= 3 {
		f.guardCounts[guardKey(command, args[1], args[2:])]--
	}
	return nil
}

func (f *fakeSystem) Exists(_ context.Context, command string, args ...string) (bool, error) {
	f.checks = append(f.checks, systemCall{command: command, args: slices.Clone(args)})
	if command == "ip" && slices.Equal(args, []string{"link", "show", Tap}) {
		return f.tapExists, nil
	}
	if len(args) > 2 && args[0] == "-C" {
		return f.guardCounts[guardKey(command, args[1], args[2:])] > 0, nil
	}
	return false, nil
}

func (f *fakeSystem) WriteFile(_ context.Context, path string, contents []byte) error {
	f.writes = append(f.writes, systemCall{command: path, args: []string{string(contents)}})
	return nil
}

func guardKey(tool, parent string, rule []string) string {
	return tool + "\x00" + parent + "\x00" + strings.Join(rule, "\x00")
}

func callString(call systemCall) string {
	return call.command + " " + strings.Join(call.args, " ")
}

func findCall(calls []systemCall, predicate func(systemCall) bool) int {
	for index, call := range calls {
		if predicate(call) {
			return index
		}
	}
	return -1
}

func TestNetworkSetupBuildsTapAndStartsDisabled(t *testing.T) {
	system := newFakeSystem()
	network := newNetwork(3128, []int{8443}, system)
	if err := network.Setup(context.Background()); err != nil {
		t.Fatal(err)
	}

	firstGuard := findCall(system.calls, func(call systemCall) bool { return len(call.args) > 0 && call.args[0] == "-I" })
	firstTapChange := findCall(system.calls, func(call systemCall) bool { return call.command == "ip" && call.args[0] == "tuntap" })
	firstFlush := findCall(system.calls, func(call systemCall) bool { return len(call.args) > 0 && call.args[0] == "-F" })
	if firstGuard != 0 || firstTapChange < 0 || firstFlush < firstTapChange {
		t.Fatalf("setup order = %v; want guards first, then TAP setup, then policy changes", formattedCalls(system.calls))
	}
	for _, want := range []string{
		"ip tuntap add tap0 mode tap",
		"ip addr add 192.0.2.1/30 dev tap0",
		"ip -6 addr add fd00:fc::1/64 dev tap0 nodad",
		"ip link set tap0 mtu 1280 up",
		"iptables -t nat -A PREROUTING -p tcp --dport 9090 -j DNAT --to-destination 192.0.2.2:9090",
	} {
		if findCall(system.calls, func(call systemCall) bool { return callString(call) == want }) < 0 {
			t.Errorf("missing command %q", want)
		}
	}
	if len(system.writes) != 1 || system.writes[0].command != "/proc/sys/net/ipv4/ip_forward" || system.writes[0].args[0] != "1" {
		t.Errorf("proc writes = %v", system.writes)
	}
	if hasRulePrefix(system.calls, "iptables", "-A", "AP_TAP_INPUT", "-p", "tcp") || hasRulePrefix(system.calls, "ip6tables", "-A", "AP_TAP_INPUT", "-p", "tcp") {
		t.Fatal("setup admitted guest TCP before Enable")
	}
	assertGuardsRemoved(t, system)
}

func TestNetworkEnableKeepsParentGuardsUntilBothFamiliesAreBuilt(t *testing.T) {
	system := newFakeSystem()
	network := newNetwork(3128, []int{8443}, system)
	if err := network.Enable(context.Background()); err != nil {
		t.Fatal(err)
	}
	firstFlush := findCall(system.calls, func(call systemCall) bool { return len(call.args) > 0 && call.args[0] == "-F" })
	lastAppend := -1
	firstDelete := findCall(system.calls, func(call systemCall) bool { return len(call.args) > 0 && call.args[0] == "-D" })
	for index, call := range system.calls {
		if len(call.args) > 0 && call.args[0] == "-A" {
			lastAppend = index
		}
	}
	if firstFlush < len(firewallGuards) || firstDelete <= lastAppend {
		t.Fatalf("guard/rule order = %v; first flush %d, last append %d, first guard delete %d", formattedCalls(system.calls), firstFlush, lastAppend, firstDelete)
	}
	for _, tool := range []string{"iptables", "ip6tables"} {
		for _, port := range []string{"3128", "8443"} {
			if !hasRule(system.calls, tool, "-A", "AP_TAP_INPUT", "-p", "tcp", "--dport", port, "-j", "ACCEPT") {
				t.Errorf("%s did not allow declared port %s", tool, port)
			}
		}
		if !hasRule(system.calls, tool, "-A", "AP_TAP_INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT") {
			t.Errorf("%s did not allow established host replies", tool)
		}
		if !hasRule(system.calls, tool, "-A", "AP_TAP_FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT") || !hasRule(system.calls, tool, "-A", "AP_TAP_FORWARD", "-j", "REJECT") {
			t.Errorf("%s forwarding policy does not allow replies then reject", tool)
		}
	}
	if !hasRule(system.calls, "iptables", "-A", "AP_TAP_INGRESS", "-d", GuestV4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT") {
		t.Fatal("IPv4 ingress did not admit guest Runtime port 9090")
	}
	if hasRule(system.calls, "ip6tables", "-A", "AP_TAP_INGRESS", "-d", GuestV4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT") {
		t.Fatal("IPv4 ingress rule was installed in the IPv6 chain")
	}
	assertGuardsRemoved(t, system)
}

func TestNetworkFailureLeavesParentGuardsInstalled(t *testing.T) {
	system := newFakeSystem()
	commandFailure := errors.New("iptables rejected an update")
	system.fail = func(command string, args []string) error {
		if command == "iptables" && slices.Equal(args, []string{"-A", "AP_TAP_INPUT", "-p", "tcp", "--dport", "3128", "-j", "ACCEPT"}) {
			return commandFailure
		}
		return nil
	}
	network := newNetwork(3128, []int{8443}, system)
	err := network.Enable(context.Background())
	if !errors.Is(err, commandFailure) {
		t.Fatalf("Enable error = %v, want wrapped command error", err)
	}
	if findCall(system.calls, func(call systemCall) bool { return len(call.args) > 0 && call.args[0] == "-D" }) >= 0 {
		t.Fatalf("failed update removed parent guards: %v", formattedCalls(system.calls))
	}
	for _, guard := range firewallGuards {
		if system.guardCounts[guardKey(guard.tool, guard.parent, guard.rule)] != 1 {
			t.Errorf("guard %s %s %v count = %d", guard.tool, guard.parent, guard.rule, system.guardCounts[guardKey(guard.tool, guard.parent, guard.rule)])
		}
	}
}

func TestNetworkDisableRejectsGuestTrafficForBothFamilies(t *testing.T) {
	system := newFakeSystem()
	network := newNetwork(3128, []int{8443}, system)
	if err := network.Disable(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, tool := range []string{"iptables", "ip6tables"} {
		if !hasRule(system.calls, tool, "-A", "AP_TAP_INPUT", "-j", "REJECT") || !hasRule(system.calls, tool, "-A", "AP_TAP_FORWARD", "-j", "REJECT") || !hasRule(system.calls, tool, "-A", "AP_TAP_INGRESS", "-j", "REJECT") {
			t.Errorf("%s disabled policy lacks a reject tail", tool)
		}
		if hasRulePrefix(system.calls, tool, "-A", "AP_TAP_INPUT", "-p", "tcp") || hasRulePrefix(system.calls, tool, "-A", "AP_TAP_INGRESS", "-d", GuestV4) {
			t.Errorf("%s disabled policy retains an allow rule", tool)
		}
	}
	assertGuardsRemoved(t, system)
}

func hasRule(calls []systemCall, tool string, expected ...string) bool {
	for _, call := range calls {
		if call.command == tool && slices.Equal(call.args, expected) {
			return true
		}
	}
	return false
}

func hasRulePrefix(calls []systemCall, tool string, prefix ...string) bool {
	for _, call := range calls {
		if call.command == tool && len(call.args) >= len(prefix) && slices.Equal(call.args[:len(prefix)], prefix) {
			return true
		}
	}
	return false
}

func assertGuardsRemoved(t *testing.T, system *fakeSystem) {
	t.Helper()
	for _, count := range system.guardCounts {
		if count != 0 {
			t.Errorf("parent guard remains installed with count %d", count)
		}
	}
}

func formattedCalls(calls []systemCall) []string {
	result := make([]string, len(calls))
	for index, call := range calls {
		result[index] = callString(call)
	}
	return result
}
