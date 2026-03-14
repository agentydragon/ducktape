// Binary kubespan is the PID-1 init for 2-node KubeSpan test VMs.
// Handles flat, cross_subnet, and discovery_only topologies.
package main

import (
	"fmt"
	"os"
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
	topology := params["topology"]
	if topology == "" {
		topology = "flat"
	}

	if initlib.Role == "" || initlib.Role == "unknown" || clusterID == "" || sharedSecret == "" || discovery == "" {
		initlib.EmitEvent(qemu_tests.Event{
			Type: qemu_tests.EventError, Message: "missing kernel cmdline params",
			Error: fmt.Sprintf("role=%s cluster_id=%s discovery=%s", initlib.Role, clusterID, discovery),
		})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventBoot, Message: fmt.Sprintf("kubespan mode, role=%s, topology=%s", initlib.Role, topology)})

	// Assign addresses based on role and topology.
	var linkIP, peerBridgeIP, peerSubnet string
	var endpointFilters []string
	var listenPort int

	switch topology {
	case "flat", "discovery_only":
		switch initlib.Role {
		case "a":
			linkIP = "192.168.50.1"
			peerBridgeIP = "192.168.50.2"
			listenPort = 51820
		case "b":
			linkIP = "192.168.50.2"
			peerBridgeIP = "192.168.50.1"
			listenPort = 51821
		default:
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: fmt.Sprintf("unknown role=%s for topology=%s", initlib.Role, topology)})
			initlib.Poweroff()
		}
		endpointFilters = []string{"192.168.50.0/24"}
	case "cross_subnet":
		switch initlib.Role {
		case "a":
			linkIP = "10.1.0.1"
			peerBridgeIP = "10.2.0.1"
			peerSubnet = "10.2.0.0/24"
			listenPort = 51820
		case "b":
			linkIP = "10.2.0.1"
			peerBridgeIP = "10.1.0.1"
			peerSubnet = "10.1.0.0/24"
			listenPort = 51821
		default:
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: fmt.Sprintf("unknown role=%s for topology=%s", initlib.Role, topology)})
			initlib.Poweroff()
		}
		endpointFilters = []string{"10.0.0.0/8"}
	default:
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: fmt.Sprintf("unknown topology=%s", topology)})
		initlib.Poweroff()
	}

	kubespanlib.LoadModules()
	kubespanlib.ConfigureNetwork(linkIP, "24")

	// Topology-specific routing.
	if topology == "cross_subnet" {
		initlib.MustRun("ip", "route", "add", peerSubnet, "dev", "eth0")
		os.WriteFile("/proc/sys/net/ipv4/conf/eth0/rp_filter", []byte("1"), 0o644)
		os.WriteFile("/proc/sys/net/ipv4/conf/default/rp_filter", []byte("0"), 0o644)
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventNetwork, Message: fmt.Sprintf("link=%s/24, topology=%s", linkIP, topology)})

	kubespandCmd := kubespanlib.StartKubespand(kubespanlib.KubespandConfig{
		ClusterID:       clusterID,
		SharedSecret:    sharedSecret,
		DiscoveryAddr:   discovery,
		ListenPort:      listenPort,
		EndpointFilters: endpointFilters,
	})

	const probePort = 9999

	peerAddr := kubespanlib.WaitForPeer(kubespandCmd)
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDiscovery, Message: "peer discovered", PeerAddr: peerAddr, PeerIPv4: peerBridgeIP})

	if initlib.Role == "b" {
		cancel := kubespanlib.ServeTCP(probePort)
		defer cancel()
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: fmt.Sprintf("role=b listening on tcp/%d, waiting (180s max)", probePort)})
		time.Sleep(180 * time.Second)
	} else {
		kubespanlib.RunProbes(peerAddr, peerBridgeIP, probePort)
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "probes completed"})
	}

	kubespandCmd.Process.Kill()
	initlib.Poweroff()
}
