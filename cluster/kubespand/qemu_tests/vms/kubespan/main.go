// Binary kubespan is the PID-1 init for KubeSpan test VMs.
// Handles flat and cross_subnet topologies.
// Optionally enables trustd CSR flow when ca_crt + token are provided;
// kubespand manages the apid subprocess internally.
package main

import (
	"encoding/base64"
	"fmt"
	"log"
	"os"

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

	log.Printf("kubespan mode, role=%s, topology=%s", initlib.Role, topology)

	// Assign addresses based on role and topology.
	var linkIP, peerSubnet string
	var endpointFilters []string
	var listenPort int

	switch topology {
	case "flat":
		switch initlib.Role {
		case "a":
			linkIP = "192.168.50.1"
			listenPort = 51820
		case "b":
			linkIP = "192.168.50.2"
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
			peerSubnet = "10.2.0.0/24"
			listenPort = 51820
		case "b":
			linkIP = "10.2.0.1"
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

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.ConfigureMgmtNIC(false)

	log.Printf("network ready: link=%s/24, topology=%s", linkIP, topology)

	cfg := kubespanlib.KubespandConfig{
		ClusterID:       clusterID,
		SharedSecret:    sharedSecret,
		DiscoveryAddr:   discovery,
		ListenPort:      listenPort,
		EndpointFilters: endpointFilters,
		ListenTCP:       params["listen_tcp"],
	}

	// If ca_crt + token are provided, enable the trustd CSR flow.
	// kubespand manages apid as a subprocess (waits for secrets.API, then starts apid).
	if caCrtB64 := params["ca_crt"]; caCrtB64 != "" {
		caCrtPEM, err := base64.StdEncoding.DecodeString(caCrtB64)
		if err != nil {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "ca_crt base64 decode failed", Error: err.Error()})
			initlib.Poweroff()
		}
		cfg.ClusterEndpoint = params["cluster_endpoint"]
		cfg.CACrt = string(caCrtPEM)
		cfg.Token = params["token"]
		cfg.ApidPath = "/apid"
	}

	kubespanlib.StartKubespand(cfg)

	const probePort = 9999
	cancel := kubespanlib.ServeTCP(probePort)
	defer cancel()

	// Start probe gRPC server on the mgmt NIC for test host control.
	initlib.StartProbeServer(fmt.Sprintf(":%d", initlib.ProbeServerPort))

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventReady, Message: fmt.Sprintf("role=%s ready, tcp/%d, probe/%d", initlib.Role, probePort, initlib.ProbeServerPort)})

	// Idle until the test host kills the VM.
	select {}
}
