// Binary kubespan is the PID-1 init for KubeSpan test VMs.
// Handles flat and cross_subnet topologies.
// Kubespand agent config is provided via a CIDATA virtio drive.
package main

import (
	"log"
	"os"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/kubespanlib"
)

func main() {
	params := initlib.Init()

	linkIP := params["link_ip"]
	if linkIP == "" {
		log.Fatalf("missing link_ip kernel parameter")
	}
	peerSubnet := params["peer_subnet"]

	log.Printf("kubespan mode, role=%s, link_ip=%s, peer_subnet=%s", initlib.Role, linkIP, peerSubnet)

	kubespanlib.LoadModules()
	kubespanlib.ConfigureNetwork(linkIP, "24")

	if peerSubnet != "" {
		initlib.MustRun("ip", "route", "add", peerSubnet, "dev", "eth0")
		os.WriteFile("/proc/sys/net/ipv4/conf/eth0/rp_filter", []byte("1"), 0o644)
		os.WriteFile("/proc/sys/net/ipv4/conf/default/rp_filter", []byte("0"), 0o644)
	}

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.ConfigureMgmtNIC(false)

	log.Printf("network ready: link=%s/24", linkIP)

	kubespanlib.RunKubespandAndIdle(9999)
}
