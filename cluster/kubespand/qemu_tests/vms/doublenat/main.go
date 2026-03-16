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
	params := initlib.Init()

	linkIP := params["link_ip"]
	if linkIP == "" {
		log.Fatalf("missing link_ip kernel parameter")
	}
	defaultGW := params["default_gw"]

	log.Printf("doublenat mode, role=%s, link_ip=%s, default_gw=%s", initlib.Role, linkIP, defaultGW)

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
