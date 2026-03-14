// Binary router is the PID-1 init process for NAT router QEMU VMs.
// Sets up masquerade on eth0 and IP forwarding between eth0 and eth1.
package main

import (
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/google/nftables"
	"github.com/google/nftables/expr"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

func main() {
	initlib.InitBasic()
	params := initlib.ParseCmdline()
	if v, ok := params["role"]; ok {
		initlib.Role = v
	}

	internetIP := params["internet_ip"]
	lanIP := params["lan_ip"]
	if internetIP == "" || lanIP == "" {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "missing internet_ip or lan_ip", Error: fmt.Sprintf("internet_ip=%s lan_ip=%s", internetIP, lanIP)})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventBoot, Message: fmt.Sprintf("router mode, internet=%s, lan=%s", internetIP, lanIP)})

	// Load nftables modules (common to all modes except discovery).
	initlib.LoadNftablesModules()

	// Load NAT-related kernel modules.
	for _, mod := range []string{"nf_conntrack", "nf_nat", "nft_masq", "nft_chain_nat"} {
		initlib.RunSilent("modprobe", mod)
	}
	initlib.RunSilent("modprobe", "virtio_net")

	// Configure eth0 (internet bridge).
	initlib.WaitForInterface("eth0")
	initlib.MustRun("ip", "link", "set", "lo", "up")
	initlib.MustRun("ip", "link", "set", "eth0", "up")
	initlib.MustRun("ip", "addr", "add", internetIP, "dev", "eth0")

	// Configure eth1 (LAN bridge).
	initlib.WaitForInterface("eth1")
	initlib.MustRun("ip", "link", "set", "eth1", "up")
	initlib.MustRun("ip", "addr", "add", lanIP, "dev", "eth1")

	// Enable IP forwarding.
	os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644)

	// Set up nftables masquerade on eth0.
	conn, err := nftables.New()
	if err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "nftables.New() failed", Error: err.Error()})
		initlib.Poweroff()
	}
	table := conn.AddTable(&nftables.Table{Family: nftables.TableFamilyIPv4, Name: "nat"})
	chain := conn.AddChain(&nftables.Chain{
		Name:     "postrouting",
		Table:    table,
		Type:     nftables.ChainTypeNAT,
		Hooknum:  nftables.ChainHookPostrouting,
		Priority: nftables.ChainPriorityNATSource,
	})
	conn.AddRule(&nftables.Rule{
		Table: table,
		Chain: chain,
		Exprs: []expr.Any{
			// Match oifname == "eth0".
			&expr.Meta{Key: expr.MetaKeyOIFNAME, Register: 1},
			&expr.Cmp{
				Op:       expr.CmpOpEq,
				Register: 1,
				Data:     []byte("eth0\x00"),
			},
			&expr.Masq{},
		},
	})
	if err := conn.Flush(); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "nftables flush failed", Error: err.Error()})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventNetwork, Message: fmt.Sprintf("router ready, internet=%s, lan=%s", internetIP, lanIP)})
	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "router running"})

	// Block until killed. Using signal.Notify avoids Go's deadlock
	// detector, which would panic on select{} with no other goroutines.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	<-sigCh
}
