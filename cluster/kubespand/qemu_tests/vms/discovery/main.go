// Binary discovery is the PID-1 init process for discovery service QEMU VMs.
// Starts the discovery-service and polls until ready.
package main

import (
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"time"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

func main() {
	initlib.InitBasic()
	params := initlib.ParseCmdline()
	if v, ok := params["role"]; ok {
		initlib.Role = v
	}

	discoveryIP := params["discovery_ip"]
	if discoveryIP == "" {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "missing discovery_ip", Error: "discovery_ip parameter required"})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventBoot, Message: fmt.Sprintf("discovery mode, ip=%s", discoveryIP)})

	initlib.RunSilent("modprobe", "virtio_net")

	// Configure eth0.
	initlib.WaitForInterface("eth0")
	initlib.MustRun("ip", "link", "set", "lo", "up")
	initlib.MustRun("ip", "link", "set", "eth0", "up")
	initlib.MustRun("ip", "addr", "add", discoveryIP, "dev", "eth0")

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventNetwork, Message: fmt.Sprintf("network ready, ip=%s", discoveryIP)})

	// Start discovery service.
	logFile, _ := os.Create("/tmp/discovery-service.log")
	discCmd := exec.Command("/discovery-service", "-debug")
	discCmd.Stdout = logFile
	discCmd.Stderr = logFile
	if err := discCmd.Start(); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "discovery-service failed to start", Error: err.Error()})
		initlib.Poweroff()
	}
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: fmt.Sprintf("discovery-service started pid=%d", discCmd.Process.Pid)})

	// Poll until ready.
	for i := 0; i < 60; i++ {
		resp, err := http.Get("http://127.0.0.1:3000/")
		if err == nil {
			resp.Body.Close()
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "discovery-service running"})

	// Sleep forever (discovery service stays up until killed).
	select {}
}
