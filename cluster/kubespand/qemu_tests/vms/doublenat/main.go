// Binary doublenat is the PID-1 init for double-NAT KubeSpan test VMs.
// Handles the 3-node topology: vps, nat1 (listener), nat2 (prober).
package main

import (
	"fmt"
	"log"

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

	log.Printf("doublenat mode, role=%s", initlib.Role)

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

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.ConfigureMgmtNIC(false)

	if defaultGW != "" {
		initlib.MustRun("ip", "route", "add", "default", "via", defaultGW)
	}

	log.Printf("network ready: link=%s/24, gw=%s", linkIP, defaultGW)

	// Basic connectivity test: can we reach the discovery service?
	initlib.Run("ping", "-c", "1", "-W", "3", "192.168.50.254")
	// Can we reach VPS?
	initlib.Run("ping", "-c", "1", "-W", "3", "192.168.50.2")

	kubespanlib.StartKubespand(kubespanlib.KubespandConfig{
		ClusterID:     clusterID,
		SharedSecret:  sharedSecret,
		DiscoveryAddr: discovery,
		ListenTCP:     params["listen_tcp"],
	})

	const probePort = 9999
	cancel := kubespanlib.ServeTCP(probePort)
	defer cancel()

	// Start probe gRPC server on the mgmt NIC for test host control.
	initlib.StartProbeServer(fmt.Sprintf(":%d", initlib.ProbeServerPort))

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventReady, Message: fmt.Sprintf("role=%s ready, tcp/%d, probe/%d", initlib.Role, probePort, initlib.ProbeServerPort)})

	// Idle until the test host kills the VM.
	select {}
}
