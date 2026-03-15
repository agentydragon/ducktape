// Package kubespanlib provides shared helpers for VM init binaries that run kubespand.
package kubespanlib

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"gopkg.in/yaml.v3"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

// KubespandConfig holds the parameters for starting kubespand.
type KubespandConfig struct {
	ClusterID       string
	SharedSecret    string
	DiscoveryAddr   string
	ListenPort      int
	EndpointFilters []string
}

// LoadModules loads wireguard, virtio_net, and nftables kernel modules.
func LoadModules() {
	initlib.LoadNftablesModules()
	if err := initlib.RunSilent("modprobe", "wireguard"); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "modprobe wireguard failed", Error: err.Error()})
	}
	initlib.RunSilent("modprobe", "virtio_net")
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventModules, Message: "all modules loaded"})
}

// ConfigureNetwork sets up eth0 with the given IP and enables ip_forward + loose rp_filter.
func ConfigureNetwork(linkIP, linkMask string) {
	initlib.MustRun("ip", "link", "set", "lo", "up")
	initlib.WaitForInterface("eth0")
	initlib.MustRun("ip", "link", "set", "eth0", "up")
	initlib.MustRun("ip", "addr", "add", linkIP+"/"+linkMask, "dev", "eth0")

	os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644)
	os.WriteFile("/proc/sys/net/ipv4/conf/all/rp_filter", []byte("2"), 0o644)
	os.WriteFile("/proc/sys/net/ipv4/conf/default/rp_filter", []byte("2"), 0o644)
}

// StartKubespand writes the config and starts kubespand in the background.
// Returns the running process command (for checking if it crashed).
func StartKubespand(cfg KubespandConfig) *exec.Cmd {
	os.MkdirAll("/var/lib/kubespan", 0o755)
	os.MkdirAll("/etc/kubespan", 0o755)

	listenPort := cfg.ListenPort
	if listenPort == 0 {
		listenPort = constants.KubeSpanDefaultPort
	}

	agentCfg := agentconfig.AgentConfig{
		Cluster: agentconfig.ClusterConfig{
			ID:     cfg.ClusterID,
			Secret: cfg.SharedSecret,
		},
		Discovery: agentconfig.DiscoveryConfig{
			Endpoint:    cfg.DiscoveryAddr,
			Insecure:    true,
			MachineType: "worker",
		},
		Kubespan: agentconfig.KubespanConfig{
			ForceRouting:          true,
			ListenPort:            listenPort,
			MTU:                   1420,
			IdentityFile:          "/var/lib/kubespan/identity.yaml",
			EndpointFilters:       cfg.EndpointFilters,
			HarvestExtraEndpoints: true,
		},
	}
	configData, err := yaml.Marshal(agentCfg)
	if err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "yaml marshal failed", Error: err.Error()})
		initlib.Poweroff()
	}
	os.WriteFile("/etc/kubespan/agent.yaml", configData, 0o644)
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "config written"})

	logFile, _ := os.Create("/tmp/kubespand.log")
	cmd := exec.Command("/kubespand", "-config", "/etc/kubespan/agent.yaml", "-debug")
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "kubespand failed to start", Error: err.Error()})
		initlib.Poweroff()
	}
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: fmt.Sprintf("started pid=%d", cmd.Process.Pid)})
	return cmd
}

// WaitForPeer waits for a single peer to be discovered in kubespand logs.
func WaitForPeer(kubespandCmd *exec.Cmd) string {
	addrs := WaitForPeers(kubespandCmd, 1)
	return addrs[0]
}

// WaitForPeers waits for n peers to be discovered in kubespand logs.
func WaitForPeers(kubespandCmd *exec.Cmd, n int) []string {
	deadline := time.Now().Add(180 * time.Second)
	for time.Now().Before(deadline) {
		if kubespandCmd.ProcessState != nil {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "kubespand exited prematurely", Error: "kubespand crashed"})
			initlib.DumpLog("/tmp/kubespand.log")
			initlib.Poweroff()
		}
		addrs := ExtractPeerAddrs("/tmp/kubespand.log")
		if len(addrs) >= n {
			return addrs
		}
		time.Sleep(2 * time.Second)
	}
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: fmt.Sprintf("timed out waiting for %d peers (180s)", n), Error: "peer discovery timeout"})
	initlib.DumpLog("/tmp/kubespand.log")
	kubespandCmd.Process.Kill()
	initlib.Poweroff()
	return nil
}

// DumpDiagnostics logs routing, WireGuard, nftables, and rp_filter state
// for debugging connectivity issues. For each peerIP, it also dumps route
// lookups with and without the KubeSpan fwmark.
func DumpDiagnostics(peerIPs ...string) {
	initlib.Run("ip", "addr", "show")
	initlib.Run("ip", "rule", "show")
	initlib.Run("ip", "route", "show", "table", "main")
	initlib.Run("ip", "route", "show", "table", "180")
	for _, ip := range peerIPs {
		initlib.Run("ip", "route", "get", ip)
		initlib.Run("ip", "route", "get", ip, "mark", "0x40")
	}
	initlib.Run("wg", "show", "kubespan")
	initlib.Run("nft", "list", "ruleset")
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/all/rp_filter")
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/kubespan/rp_filter")
}

// ExtractPeerAddrs parses kubespand log for discovered peer ULA addresses.
func ExtractPeerAddrs(logPath string) []string {
	f, err := os.Open(logPath)
	if err != nil {
		return nil
	}
	defer f.Close()
	seen := map[string]struct{}{}
	var addrs []string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.Contains(line, "configuring peer") {
			continue
		}
		if idx := strings.Index(line, `"address": "`); idx >= 0 {
			rest := line[idx+len(`"address": "`):]
			if end := strings.IndexByte(rest, '"'); end >= 0 {
				addr := rest[:end]
				if _, ok := seen[addr]; !ok {
					seen[addr] = struct{}{}
					addrs = append(addrs, addr)
				}
			}
		}
	}
	return addrs
}
