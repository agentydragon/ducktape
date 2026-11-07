package firecracker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

const (
	GuestV4   = "192.0.2.2"
	Tap       = "tap0"
	gatewayV4 = "192.0.2.1"
	gatewayV6 = "fd00:fc::1"
)

type systemCommands interface {
	Run(ctx context.Context, command string, args ...string) error
	Exists(ctx context.Context, command string, args ...string) (bool, error)
	WriteFile(ctx context.Context, path string, contents []byte) error
}

type hostSystem struct{}

func (hostSystem) Run(ctx context.Context, command string, args ...string) error {
	cmd := exec.CommandContext(ctx, command, args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s %s: %w: %s", command, strings.Join(args, " "), err, strings.TrimSpace(string(output)))
	}
	return nil
}

func (hostSystem) Exists(ctx context.Context, command string, args ...string) (bool, error) {
	cmd := exec.CommandContext(ctx, command, args...)
	output, err := cmd.CombinedOutput()
	if err == nil {
		return true, nil
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) && exitError.ExitCode() == 1 {
		return false, nil
	}
	return false, fmt.Errorf("%s %s: %w: %s", command, strings.Join(args, " "), err, strings.TrimSpace(string(output)))
}

func (hostSystem) WriteFile(_ context.Context, path string, contents []byte) error {
	if err := os.WriteFile(path, contents, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}

type Network struct {
	relayPort     int
	platformPorts []int
	system        systemCommands
}

func NewNetwork(relayPort int, platformPorts []int) *Network {
	return &Network{
		relayPort:     relayPort,
		platformPorts: append([]int(nil), platformPorts...),
		system:        hostSystem{},
	}
}

func newNetwork(relayPort int, platformPorts []int, system systemCommands) *Network {
	return &Network{
		relayPort:     relayPort,
		platformPorts: append([]int(nil), platformPorts...),
		system:        system,
	}
}

type firewallGuard struct {
	tool   string
	parent string
	rule   []string
}

var firewallGuards = []firewallGuard{
	{tool: "iptables", parent: "INPUT", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{tool: "iptables", parent: "FORWARD", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{tool: "iptables", parent: "FORWARD", rule: []string{"-o", Tap, "-j", "REJECT"}},
	{tool: "ip6tables", parent: "INPUT", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{tool: "ip6tables", parent: "FORWARD", rule: []string{"-i", Tap, "-j", "REJECT"}},
	{tool: "ip6tables", parent: "FORWARD", rule: []string{"-o", Tap, "-j", "REJECT"}},
}

func (n *Network) Setup(ctx context.Context) error {
	return n.withGuards(ctx, func() error {
		if exists, err := n.system.Exists(ctx, "ip", "link", "show", Tap); err != nil {
			return err
		} else if exists {
			if err := n.system.Run(ctx, "ip", "link", "del", Tap); err != nil {
				return err
			}
		}
		for _, args := range [][]string{
			{"tuntap", "add", Tap, "mode", "tap"},
			{"addr", "add", gatewayV4 + "/30", "dev", Tap},
			{"-6", "addr", "add", gatewayV6 + "/64", "dev", Tap, "nodad"},
			{"link", "set", Tap, "mtu", "1280", "up"},
		} {
			if err := n.system.Run(ctx, "ip", args...); err != nil {
				return err
			}
		}
		if err := n.system.WriteFile(ctx, "/proc/sys/net/ipv4/ip_forward", []byte("1")); err != nil {
			return err
		}
		if err := n.ensureFirewall(ctx); err != nil {
			return err
		}
		return n.writeRules(ctx, false)
	})
}

func (n *Network) Enable(ctx context.Context) error {
	return n.withGuards(ctx, func() error { return n.writeRules(ctx, true) })
}

func (n *Network) Disable(ctx context.Context) error {
	return n.withGuards(ctx, func() error { return n.writeRules(ctx, false) })
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
		args := []string{"-I", guard.parent, "1"}
		args = append(args, guard.rule...)
		if err := n.system.Run(ctx, guard.tool, args...); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func (n *Network) removeGuards(ctx context.Context) error {
	for _, guard := range firewallGuards {
		check := []string{"-C", guard.parent}
		check = append(check, guard.rule...)
		for {
			exists, err := n.system.Exists(ctx, guard.tool, check...)
			if err != nil {
				return err
			}
			if !exists {
				break
			}
			remove := []string{"-D", guard.parent}
			remove = append(remove, guard.rule...)
			if err := n.system.Run(ctx, guard.tool, remove...); err != nil {
				return err
			}
		}
	}
	return nil
}

func (n *Network) ensureFirewall(ctx context.Context) error {
	for _, tool := range []string{"iptables", "ip6tables"} {
		for _, chain := range []string{"AP_TAP_INPUT", "AP_TAP_FORWARD", "AP_TAP_INGRESS"} {
			exists, err := n.system.Exists(ctx, tool, "-S", chain)
			if err != nil {
				return err
			}
			if !exists {
				if err := n.system.Run(ctx, tool, "-N", chain); err != nil {
					return err
				}
			}
		}
		for _, jump := range []struct {
			chain     string
			direction string
			parent    string
		}{
			{chain: "AP_TAP_INPUT", direction: "-i", parent: "INPUT"},
			{chain: "AP_TAP_FORWARD", direction: "-i", parent: "FORWARD"},
			{chain: "AP_TAP_INGRESS", direction: "-o", parent: "FORWARD"},
		} {
			rule := []string{jump.direction, Tap, "-j", jump.chain}
			exists, err := n.system.Exists(ctx, tool, append([]string{"-C", jump.parent}, rule...)...)
			if err != nil {
				return err
			}
			if !exists {
				if err := n.system.Run(ctx, tool, append([]string{"-A", jump.parent}, rule...)...); err != nil {
					return err
				}
			}
		}
	}
	natRule := []string{"-p", "tcp", "--dport", "9090", "-j", "DNAT", "--to-destination", GuestV4 + ":9090"}
	exists, err := n.system.Exists(ctx, "iptables", append([]string{"-t", "nat", "-C", "PREROUTING"}, natRule...)...)
	if err != nil {
		return err
	}
	if !exists {
		if err := n.system.Run(ctx, "iptables", append([]string{"-t", "nat", "-A", "PREROUTING"}, natRule...)...); err != nil {
			return err
		}
	}
	return nil
}

func (n *Network) writeRules(ctx context.Context, enabled bool) error {
	for _, tool := range []string{"iptables", "ip6tables"} {
		for _, chain := range []string{"AP_TAP_INPUT", "AP_TAP_FORWARD", "AP_TAP_INGRESS"} {
			if err := n.system.Run(ctx, tool, "-F", chain); err != nil {
				return err
			}
		}
		if tool == "ip6tables" {
			for _, kind := range []string{"neighbor-solicitation", "neighbor-advertisement"} {
				if err := n.system.Run(
					ctx, tool,
					"-A", "AP_TAP_INPUT", "-p", "ipv6-icmp", "--icmpv6-type", kind,
					"-m", "hl", "--hl-eq", "255", "-j", "ACCEPT",
				); err != nil {
					return err
				}
			}
		}
		if enabled {
			if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"); err != nil {
				return err
			}
			for _, port := range append([]int{n.relayPort}, n.platformPorts...) {
				if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INPUT", "-p", "tcp", "--dport", strconv.Itoa(port), "-j", "ACCEPT"); err != nil {
					return err
				}
			}
		}
		if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INPUT", "-j", "REJECT"); err != nil {
			return err
		}
		if enabled {
			if err := n.system.Run(ctx, tool, "-A", "AP_TAP_FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"); err != nil {
				return err
			}
		}
		if err := n.system.Run(ctx, tool, "-A", "AP_TAP_FORWARD", "-j", "REJECT"); err != nil {
			return err
		}
		if enabled && tool == "iptables" {
			if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INGRESS", "-d", GuestV4, "-p", "tcp", "--dport", "9090", "-j", "ACCEPT"); err != nil {
				return err
			}
		}
		if enabled {
			if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INGRESS", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"); err != nil {
				return err
			}
		}
		if err := n.system.Run(ctx, tool, "-A", "AP_TAP_INGRESS", "-j", "REJECT"); err != nil {
			return err
		}
	}
	return nil
}
