// Binary discovery is the PID-1 init process for discovery service QEMU VMs.
// Starts the discovery-service and polls until ready.
package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"time"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

func main() {
	params := initlib.Init()

	discoveryIP := params["discovery_ip"]
	if discoveryIP == "" {
		log.Fatalf("missing discovery_ip parameter")
	}

	log.Printf("discovery mode, ip=%s", discoveryIP)

	initlib.RunSilent("modprobe", "virtio_net")

	// Configure eth0.
	initlib.WaitForInterface("eth0")
	initlib.MustRun("ip", "link", "set", "lo", "up")
	initlib.MustRun("ip", "link", "set", "eth0", "up")
	initlib.MustRun("ip", "addr", "add", discoveryIP, "dev", "eth0")

	// In cross_subnet topology, the discovery VM needs routes to all
	// peer subnets (e.g. 10.2.0.0/24) so it can respond to gRPC
	// connections from VMs on different subnets sharing the same L2.
	if params["topology"] == "cross_subnet" {
		initlib.MustRun("ip", "route", "add", "10.0.0.0/8", "dev", "eth0")
	}

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	// Optional — only present when the test adds a hostfwd NIC.
	initlib.ConfigureMgmtNIC(false)

	log.Printf("network ready, ip=%s", discoveryIP)

	// Start discovery service.
	logFile, _ := os.Create("/tmp/discovery-service.log")
	discCmd := exec.Command("/discovery-service", "-debug")
	discCmd.Stdout = logFile
	discCmd.Stderr = logFile
	if err := discCmd.Start(); err != nil {
		log.Fatalf("discovery-service failed to start: %v", err)
	}
	log.Printf("discovery-service started pid=%d", discCmd.Process.Pid)

	// Poll until ready.
	for i := 0; i < 60; i++ {
		resp, err := http.Get("http://127.0.0.1:3000/")
		if err == nil {
			resp.Body.Close()
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	// Start probe gRPC server on the mgmt NIC for test host diagnostics.
	// The test host polls this server to detect VM readiness.
	initlib.StartProbeServer(fmt.Sprintf(":%d", initlib.ProbeServerPort))

	log.Printf("discovery-service running")

	// Block until the discovery service exits (or the VM is killed).
	// Using discCmd.Wait() instead of select{} avoids Go's deadlock
	// detector, which would panic because no other goroutines are running.
	if err := discCmd.Wait(); err != nil {
		log.Printf("discovery-service exited: %v", err)
	}
	initlib.Poweroff()
}
