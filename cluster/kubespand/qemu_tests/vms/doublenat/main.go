// Binary doublenat is the PID-1 init for double-NAT KubeSpan test VMs.
// Handles the 3-node topology: vps, nat1 (listener), nat2 (prober).
// Kubespand agent config is provided via a CIDATA virtio drive.
package main

import (
	"log"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/kubespanlib"
)

func main() {
	_ = initlib.Init()
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
		log.Fatalf("unknown role=%s", initlib.Role)
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

	kubespanlib.RunKubespandAndIdle(9999)
}
