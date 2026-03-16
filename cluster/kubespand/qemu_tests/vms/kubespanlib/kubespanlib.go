// Package kubespanlib provides shared helpers for VM init binaries that run kubespand.
package kubespanlib

import (
	"fmt"
	"log"
	"os"
	"os/exec"

	"gopkg.in/yaml.v3"

	"github.com/siderolabs/talos/pkg/machinery/constants"

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
	// ClusterEndpoint is the cluster API server URL (for trustd endpoint discovery).
	ClusterEndpoint string
	// CACrt is the PEM-encoded Talos CA certificate (for trustd CSR flow).
	CACrt string
	// Token is the Talos machine token (for trustd authentication).
	Token string
	// ApidPath is the path to the apid binary. When set, kubespand manages apid
	// as a subprocess (waits for secrets.API, then starts apid).
	ApidPath string
	// ListenTCP is a TCP address for the read-only COSI API (e.g., ":50100").
	ListenTCP string
	// CertSANs are additional IPs or DNS names for the apid TLS certificate.
	CertSANs []string
}

// LoadModules loads wireguard, virtio_net, and nftables kernel modules.
func LoadModules() {
	initlib.LoadNftablesModules()
	if err := initlib.RunSilent("modprobe", "wireguard"); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "modprobe wireguard failed", Error: err.Error()})
	}
	initlib.RunSilent("modprobe", "virtio_net")
	log.Printf("all modules loaded")
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
			ID:       cfg.ClusterID,
			Secret:   cfg.SharedSecret,
			Endpoint: cfg.ClusterEndpoint,
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
		Api: agentconfig.ApiConfig{
			CACrt:     cfg.CACrt,
			Token:     cfg.Token,
			ApidPath:  cfg.ApidPath,
			ListenTCP: cfg.ListenTCP,
			CertSANs:  cfg.CertSANs,
		},
	}
	configData, err := yaml.Marshal(agentCfg)
	if err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "yaml marshal failed", Error: err.Error()})
		initlib.Poweroff()
	}
	os.WriteFile("/etc/kubespan/agent.yaml", configData, 0o644)
	log.Printf("kubespand config written")

	logFile, _ := os.Create("/tmp/kubespand.log")
	cmd := exec.Command("/kubespand", "-config", "/etc/kubespan/agent.yaml", "-debug")
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "kubespand failed to start", Error: err.Error()})
		initlib.Poweroff()
	}
	log.Printf("kubespand started pid=%d", cmd.Process.Pid)
	return cmd
}
