// Package kubespanlib provides shared helpers for VM init binaries that run kubespand.
package kubespanlib

import (
	"fmt"
	"log"
	"os"
	"os/exec"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

// LoadModules loads wireguard, virtio_net, and nftables kernel modules.
func LoadModules() {
	initlib.LoadNftablesModules()
	if err := initlib.RunSilent("modprobe", "wireguard"); err != nil {
		log.Printf("modprobe wireguard failed: %v", err)
	}
	initlib.RunSilent("modprobe", "virtio_net")
	log.Printf("all modules loaded")
}

// ConfigureNetwork sets up eth0 with the given IP and enables ip_forward + loose rp_filter.
func ConfigureNetwork(linkIP, linkMask string) {
	initlib.MustRun("ip", "link", "set", "lo", "up")
	initlib.WaitForInterface("eth0")
	initlib.MustRun("ip", "link", "set", "eth0", "up")
	initlib.MustRun("ip", "addr", "add", linkIP+"/"+linkMask, "dev", "eth0")

	os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644)
	os.WriteFile("/proc/sys/net/ipv4/conf/all/rp_filter", []byte("2"), 0o644)
	os.WriteFile("/proc/sys/net/ipv4/conf/default/rp_filter", []byte("2"), 0o644)
}

// StartKubespand starts kubespand from the pre-existing config at
// /etc/kubespan/agent.yaml (written by initlib.MountKubespandCIDATA).
func StartKubespand() *exec.Cmd {
	logFile, _ := os.Create("/tmp/kubespand.log")
	cmd := exec.Command("/kubespand", "-config", "/etc/kubespan/agent.yaml", "-debug")
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		log.Fatalf("kubespand failed to start: %v", err)
	}
	log.Printf("kubespand started pid=%d", cmd.Process.Pid)
	return cmd
}

// RunKubespandAndIdle loads config from CIDATA, starts kubespand, starts a
// TCP probe listener on probePort, starts the gRPC probe server, then blocks
// forever. This is the common tail for kubespan and doublenat VM inits.
func RunKubespandAndIdle(probePort int) {
	initlib.MountKubespandCIDATA()
	StartKubespand()

	cancel := ServeTCP(probePort)
	defer cancel()

	initlib.StartProbeServer(fmt.Sprintf(":%d", initlib.ProbeServerPort))
	log.Printf("role=%s ready, tcp/%d, probe/%d", initlib.Role, probePort, initlib.ProbeServerPort)

	select {}
}
