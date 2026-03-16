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
	topology := params["topology"]
	if topology == "" {
		topology = "flat"
	}
	log.Printf("kubespan mode, role=%s, topology=%s", initlib.Role, topology)

	// Assign addresses based on role and topology.
	var linkIP, peerSubnet string

	switch topology {
	case "flat":
		switch initlib.Role {
		case "a":
			linkIP = "192.168.50.1"
		case "b":
			linkIP = "192.168.50.2"
		default:
			log.Fatalf("unknown role=%s for topology=%s", initlib.Role, topology)
		}
	case "cross_subnet":
		switch initlib.Role {
		case "a":
			linkIP = "10.1.0.1"
			peerSubnet = "10.2.0.0/24"
		case "b":
			linkIP = "10.2.0.1"
			peerSubnet = "10.1.0.0/24"
		default:
			log.Fatalf("unknown role=%s for topology=%s", initlib.Role, topology)
		}
	default:
		log.Fatalf("unknown topology=%s", topology)
	}

	kubespanlib.LoadModules()
	kubespanlib.ConfigureNetwork(linkIP, "24")

	// Topology-specific routing.
	if topology == "cross_subnet" {
		initlib.MustRun("ip", "route", "add", peerSubnet, "dev", "eth0")
		os.WriteFile("/proc/sys/net/ipv4/conf/eth0/rp_filter", []byte("1"), 0o644)
		os.WriteFile("/proc/sys/net/ipv4/conf/default/rp_filter", []byte("0"), 0o644)
	}

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.ConfigureMgmtNIC(false)

	log.Printf("network ready: link=%s/24, topology=%s", linkIP, topology)

	kubespanlib.RunKubespandAndIdle(9999)
}
