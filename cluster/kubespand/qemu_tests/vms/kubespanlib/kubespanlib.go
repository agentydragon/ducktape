// Package kubespanlib provides shared helpers for VM init binaries that run kubespand.
package kubespanlib

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strings"
	"time"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
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

// WaitForPeer waits for a single peer to appear via kubespand's COSI API.
func WaitForPeer(kubespandCmd *exec.Cmd) string {
	addrs := WaitForPeers(kubespandCmd, 1)
	return addrs[0]
}

// NewCOSIClient connects to kubespand's Unix socket and returns a COSI state client.
func NewCOSIClient(socketPath string) (state.State, *grpc.ClientConn, error) {
	conn, err := grpc.NewClient("passthrough:///unix",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
			return net.Dial("unix", socketPath)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("dialing %s: %w", socketPath, err)
	}
	adapter := stateclient.NewAdapter(v1alpha1.NewStateClient(conn))
	return state.WrapCore(adapter), conn, nil
}

// WaitForPeers polls kubespand's COSI API for PeerSpec resources until n peers are found.
// Returns the KubeSpan IPv6 ULA address of each peer.
func WaitForPeers(kubespandCmd *exec.Cmd, n int) []string {
	const timeout = 180 * time.Second
	deadline := time.Now().Add(timeout)
	socketPath := constants.MachineSocketPath

	lastProgressLog := time.Now()
	var cosiState state.State
	var conn *grpc.ClientConn

	for time.Now().Before(deadline) {
		if kubespandCmd.ProcessState != nil {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "kubespand exited prematurely", Error: "kubespand crashed"})
			initlib.DumpLog("/tmp/kubespand.log")
			initlib.Poweroff()
		}

		// Lazily connect to the COSI API socket.
		if cosiState == nil {
			var err error
			cosiState, conn, err = NewCOSIClient(socketPath)
			if err != nil {
				// Socket might not exist yet while kubespand starts up.
				if time.Since(lastProgressLog) > 15*time.Second {
					fmt.Fprintf(os.Stderr, "[WaitForPeers] waiting for API socket %s: %v\n", socketPath, err)
					lastProgressLog = time.Now()
				}
				time.Sleep(500 * time.Millisecond)
				continue
			}
			defer conn.Close()
		}

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		list, err := safe.StateListAll[*kubespan.PeerSpec](ctx, cosiState)
		cancel()

		if err != nil {
			if time.Since(lastProgressLog) > 15*time.Second {
				lastProgressLog = time.Now()
				remaining := time.Until(deadline).Round(time.Second)
				fmt.Fprintf(os.Stderr, "[WaitForPeers] COSI list error: %v, %s remaining\n", err, remaining)
			}
			time.Sleep(500 * time.Millisecond)
			continue
		}

		var addrs []string
		totalPeers := 0
		for it := list.Iterator(); it.Next(); {
			ps := it.Value()
			totalPeers++
			addr := ps.TypedSpec().Address.String()
			if addr != "" && addr != "invalid IP" {
				addrs = append(addrs, addr)
			}
		}

		if time.Since(lastProgressLog) > 15*time.Second {
			lastProgressLog = time.Now()
			remaining := time.Until(deadline).Round(time.Second)
			fmt.Fprintf(os.Stderr, "[WaitForPeers] found %d/%d peers (%d total PeerSpecs) via COSI API, %s remaining\n",
				len(addrs), n, totalPeers, remaining)
			// Log per-peer details for debugging kubespand↔Talos handshake issues.
			list2, err2 := safe.StateListAll[*kubespan.PeerSpec](context.Background(), cosiState)
			if err2 == nil {
				for it2 := list2.Iterator(); it2.Next(); {
					ps2 := it2.Value()
					spec := ps2.TypedSpec()
					fmt.Fprintf(os.Stderr, "[WaitForPeers]   peer label=%q addr=%s endpoints=%v\n",
						spec.Label, spec.Address, spec.Endpoints)
				}
			}
			dumpLogTail("/tmp/kubespand.log", 20)
		}

		if len(addrs) >= n {
			// Log final peer set including any additional peers (e.g., Talos CP).
			fmt.Fprintf(os.Stderr, "[WaitForPeers] SUCCESS: %d peers found (%d total PeerSpecs)\n", len(addrs), totalPeers)
			return addrs
		}

		time.Sleep(500 * time.Millisecond)
	}

	// On timeout, dump full diagnostics.
	fatalKubespandTimeout(kubespandCmd, fmt.Sprintf("timed out waiting for %d peers (%s)", n, timeout))
	return nil
}

// WaitForPeerUp polls kubespand's COSI API for PeerStatus resources until
// minPeers peers report state "up" (WireGuard handshake completed).
// This should be called after WaitForPeer to ensure the handshake is done
// before running data-plane probes and before the test host polls PeerStatus.
func WaitForPeerUp(kubespandCmd *exec.Cmd, minPeers int) {
	const timeout = 180 * time.Second
	deadline := time.Now().Add(timeout)
	socketPath := constants.MachineSocketPath

	var cosiState state.State
	var conn *grpc.ClientConn
	lastLog := time.Now()

	for time.Now().Before(deadline) {
		if kubespandCmd.ProcessState != nil {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "kubespand exited during WaitForPeerUp"})
			initlib.DumpLog("/tmp/kubespand.log")
			initlib.Poweroff()
		}

		if cosiState == nil {
			var err error
			cosiState, conn, err = NewCOSIClient(socketPath)
			if err != nil {
				time.Sleep(500 * time.Millisecond)
				continue
			}
			defer conn.Close()
		}

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, cosiState)
		cancel()

		if err != nil {
			if time.Since(lastLog) > 15*time.Second {
				fmt.Fprintf(os.Stderr, "[WaitForPeerUp] COSI error: %v\n", err)
				lastLog = time.Now()
			}
			time.Sleep(500 * time.Millisecond)
			continue
		}

		upCount := 0
		totalPeers := 0
		for it := list.Iterator(); it.Next(); {
			totalPeers++
			if it.Value().TypedSpec().State == kubespan.PeerStateUp {
				upCount++
			}
		}

		if upCount >= minPeers {
			fmt.Fprintf(os.Stderr, "[WaitForPeerUp] SUCCESS: %d/%d peers up (%d total)\n", upCount, minPeers, totalPeers)
			return
		}

		if time.Since(lastLog) > 15*time.Second {
			remaining := time.Until(deadline).Round(time.Second)
			fmt.Fprintf(os.Stderr, "[WaitForPeerUp] %d/%d peers up (%d total), %s remaining\n", upCount, minPeers, totalPeers, remaining)
			lastLog = time.Now()
		}

		time.Sleep(500 * time.Millisecond)
	}

	fatalKubespandTimeout(kubespandCmd, fmt.Sprintf("timed out waiting for %d peers up (%s)", minPeers, timeout))
}

// fatalKubespandTimeout dumps diagnostics, emits an error event, and powers off.
// Used by WaitForPeers and WaitForPeerUp on timeout.
func fatalKubespandTimeout(kubespandCmd *exec.Cmd, msg string) {
	DumpDiagnostics()
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: msg, Error: "timeout"})
	initlib.DumpLog("/tmp/kubespand.log")
	kubespandCmd.Process.Kill()
	initlib.Poweroff()
}

// dumpLogTail prints the last n lines of a log file to stderr.
func dumpLogTail(path string, n int) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	lines := strings.Split(string(data), "\n")
	start := len(lines) - n
	if start < 0 {
		start = 0
	}
	fmt.Fprintf(os.Stderr, "--- last %d lines of %s ---\n", n, path)
	for _, line := range lines[start:] {
		if line != "" {
			fmt.Fprintln(os.Stderr, line)
		}
	}
	fmt.Fprintf(os.Stderr, "--- end %s ---\n", path)
}

// DumpDiagnostics logs routing, WireGuard, nftables, and rp_filter state
// for debugging connectivity issues. For each peerIP, it also dumps route
// lookups with and without the KubeSpan fwmark.
func DumpDiagnostics(peerIPs ...string) {
	fmt.Fprintln(os.Stderr, "=== DIAGNOSTICS START ===")
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
	// Dump rp_filter for all interfaces.
	fmt.Fprintln(os.Stderr, "--- rp_filter state ---")
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/all/rp_filter")
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/default/rp_filter")
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/eth0/rp_filter")
	initlib.RunSilent("cat", "/proc/sys/net/ipv4/conf/kubespan/rp_filter")
	// Dump src_valid_mark
	initlib.Run("cat", "/proc/sys/net/ipv4/conf/all/src_valid_mark")
	// Dump ip_forward
	initlib.Run("cat", "/proc/sys/net/ipv4/ip_forward")
	fmt.Fprintln(os.Stderr, "=== DIAGNOSTICS END ===")
}
