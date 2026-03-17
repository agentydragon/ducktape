// Package kubespanlib provides shared helpers for VM init binaries that run kubespand.
package kubespanlib

import (
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

// LoadModules loads kernel modules needed by kubespand VMs: nftables, wireguard,
// virtio drivers, filesystem modules for CIDATA mounting, and modules required by
// the embedded Talos KernelParamSpecController (br_netfilter for bridge-nf-call-*
// sysctls, yama for ptrace_scope).
func LoadModules() {
	initlib.LoadNftablesModules()
	// virtio_blk: CIDATA virtio drive to appear as /dev/vda.
	// fat, vfat: mount the FAT32 CIDATA filesystem.
	// These are modules (not built-in) in the Alpine linux-virt kernel.
	for _, mod := range []string{
		"wireguard", "virtio_net", "virtio_blk", "fat", "vfat",
		"br_netfilter", "yama",
	} {
		if err := initlib.RunSilent("modprobe", mod); err != nil {
			log.Printf("modprobe %s failed: %v", mod, err)
		}
	}
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
// Output goes to stderr (VM console) so it appears in the test VM log.
func StartKubespand() *exec.Cmd {
	cmd := exec.Command("/kubespand", "-config", "/etc/kubespan/agent.yaml", "-debug")
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
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

// ServeTCP starts TCP listeners on the given port on both IPv4 and IPv6.
func ServeTCP(port int) (cancel func()) {
	addr := fmt.Sprintf(":%d", port)
	var listeners []net.Listener
	for _, network := range []string{"tcp4", "tcp6"} {
		ln, err := net.Listen(network, addr)
		if err != nil {
			fmt.Fprintf(os.Stderr, "serveTCP %s: %v\n", network, err)
			continue
		}
		listeners = append(listeners, ln)
		go func(l net.Listener) {
			for {
				conn, err := l.Accept()
				if err != nil {
					return
				}
				conn.Close()
			}
		}(ln)
	}
	return func() {
		for _, ln := range listeners {
			ln.Close()
		}
	}
}
