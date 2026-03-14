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
	var listenPort int

	// All nodes use the default KubeSpan port (51820). Each VM is on its own
	// subnet behind its own NAT router, so there are no port conflicts.
	// The upstream Talos LocalAffiliateController hardcodes KubeSpanDefaultPort
	// when constructing endpoint addresses, so the listen port must match.
	listenPort = 51820

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
		ListenPort:    listenPort,
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

		// Dump routing diagnostics before probing.
		initlib.Run("ip", "addr", "show")
		initlib.Run("ip", "rule", "show")
		initlib.Run("ip", "route", "show", "table", "180")
		initlib.Run("ip", "route", "show", "table", "main")
		initlib.Run("wg", "show", "kubespan")
		initlib.Run("nft", "list", "ruleset")
		initlib.Run("cat", "/proc/sys/net/ipv4/conf/all/rp_filter")
		initlib.Run("cat", "/proc/sys/net/ipv4/conf/kubespan/rp_filter")

		kubespanlib.RunDoubleNATProbes(peerAddrs, probePort)
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "probes completed"})
	}

	kubespandCmd.Process.Kill()
	initlib.Poweroff()
}
