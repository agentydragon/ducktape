// Binary doublenat is the PID-1 init for double-NAT KubeSpan test VMs.
// Handles the 3-node topology: vps, nat1 (listener), nat2 (prober).
package main

import (
	"fmt"
	"time"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/kubespanlib"
)

func main() {
	initlib.InitBasic()
	params := initlib.ParseCmdline()
	if v, ok := params["role"]; ok {
		initlib.Role = v
	}

	clusterID := params["cluster_id"]
	sharedSecret := params["shared_secret"]
	discovery := params["discovery"]

	if initlib.Role == "" || initlib.Role == "unknown" || clusterID == "" || sharedSecret == "" || discovery == "" {
		initlib.EmitEvent(qemu_tests.Event{
			Type: qemu_tests.EventError, Message: "missing kernel cmdline params",
			Error: fmt.Sprintf("role=%s cluster_id=%s discovery=%s", initlib.Role, clusterID, discovery),
		})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventBoot, Message: fmt.Sprintf("doublenat mode, role=%s", initlib.Role)})

	var linkIP, defaultGW string

	switch initlib.Role {
	case "vps":
		linkIP = "192.168.50.2"
	case "nat1":
		linkIP = "192.168.60.2"
		defaultGW = "192.168.60.1"
	case "nat2":
		linkIP = "192.168.70.2"
		defaultGW = "192.168.70.1"
	default:
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: fmt.Sprintf("unknown role=%s", initlib.Role)})
		initlib.Poweroff()
	}

	kubespanlib.LoadModules()
	kubespanlib.ConfigureNetwork(linkIP, "24")

	// eth1: mgmt NIC (QEMU user-mode) for port forwarding COSI API to the test host.
	if initlib.HasInterface("eth1", 2*time.Second) {
		initlib.MustRun("ip", "link", "set", "eth1", "up")
		initlib.MustRun("ip", "addr", "add", "10.0.2.15/24", "dev", "eth1")
	}

	if defaultGW != "" {
		initlib.MustRun("ip", "route", "add", "default", "via", defaultGW)
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventNetwork, Message: fmt.Sprintf("link=%s/24, gw=%s", linkIP, defaultGW)})

	// Basic connectivity test: can we reach the discovery service?
	initlib.Run("ping", "-c", "1", "-W", "3", "192.168.50.254")
	// Can we reach VPS?
	initlib.Run("ping", "-c", "1", "-W", "3", "192.168.50.2")

	kubespandCmd := kubespanlib.StartKubespand(kubespanlib.KubespandConfig{
		ClusterID:     clusterID,
		SharedSecret:  sharedSecret,
		DiscoveryAddr: discovery,
		ListenTCP:     params["listen_tcp"],
	})

	const probePort = 9999

	switch initlib.Role {
	case "vps", "nat1":
		cancel := kubespanlib.ServeTCP(probePort)
		defer cancel()
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: fmt.Sprintf("role=%s listening, waiting", initlib.Role)})
		time.Sleep(300 * time.Second)
	case "nat2":
		peerAddrs := kubespanlib.WaitForPeers(kubespandCmd, 2)
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDiscovery, Message: fmt.Sprintf("discovered %d peers", len(peerAddrs))})

		// Wait for at least 1 peer handshake before probes (VPS is directly
		// reachable; NAT1 may stay down due to endpoint-dependent filtering).
		kubespanlib.WaitForPeerUp(kubespandCmd, 1)
		kubespanlib.DumpDiagnostics()

		kubespanlib.RunDoubleNATProbes(peerAddrs, probePort)
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "probes completed"})
		// Keep kubespand alive for test host PeerStatus observation.
		time.Sleep(30 * time.Second)
	}

	kubespandCmd.Process.Kill()
	initlib.Poweroff()
}
