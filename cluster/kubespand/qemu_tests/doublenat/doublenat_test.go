package doublenat_test

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestDoubleNAT(t *testing.T) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfs := h.RunfilePath(t, h.DoublenatInitramfs)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	initramfsRouter := h.RunfilePath(t, h.RouterInitramfs)
	out := h.OutputDir(t)
	sw.Lap("resolve runfiles")

	clusterID := h.RandomBase64(32)
	sharedSecret := h.RandomBase64(32)

	mcastPortInternet := h.RandomPort()
	mcastPortLanA := h.RandomPort()
	mcastPortLanB := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLanA := fmt.Sprintf("230.0.0.1:%d", mcastPortLanA)
	mcastLanB := fmt.Sprintf("230.0.0.1:%d", mcastPortLanB)

	const discAddr = "192.168.50.254:3000"

	kubespanBase := fmt.Sprintf("mode=kubespan cluster_id=%s shared_secret=%s discovery=%s topology=double_nat",
		clusterID, sharedSecret, discAddr)

	vmDiscovery := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastInternet, "52:54:00:ff:00:01")...)
	sw.Lap("boot discovery VM")

	vmVPS := h.BootVM(t, "vm-vps", vmlinuz, initramfs, kubespanBase+" role=vps",
		h.McastNIC("net0", mcastInternet, "52:54:00:c0:00:01")...)
	sw.Lap("boot VPS VM")

	vmRouterA := h.BootVM(t, "vm-router-a", vmlinuz, initramfsRouter,
		"mode=router role=router-a internet_ip=192.168.50.1/24 lan_ip=192.168.60.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c1:00:01"),
			h.McastNIC("net1", mcastLanA, "52:54:00:c1:00:02")...)...)
	sw.Lap("boot Router-A VM")

	vmRouterB := h.BootVM(t, "vm-router-b", vmlinuz, initramfsRouter,
		"mode=router role=router-b internet_ip=192.168.50.3/24 lan_ip=192.168.70.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c2:00:01"),
			h.McastNIC("net1", mcastLanB, "52:54:00:c2:00:02")...)...)
	sw.Lap("boot Router-B VM")

	allVMs := []*h.VM{vmVPS, vmRouterA, vmRouterB, vmDiscovery}

	h.RequireAllEvents(t, []*h.VM{vmDiscovery, vmRouterA, vmRouterB}, h.EventDone, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	vmNAT1 := h.BootVM(t, "vm-nat1", vmlinuz, initramfs, kubespanBase+" role=nat1",
		h.McastNIC("net0", mcastLanA, "52:54:00:d0:00:01")...)
	sw.Lap("boot NAT1 VM")

	vmNAT2 := h.BootVM(t, "vm-nat2", vmlinuz, initramfs, kubespanBase+" role=nat2",
		h.McastNIC("net0", mcastLanB, "52:54:00:e0:00:01")...)
	sw.Lap("boot NAT2 VM")

	allVMs = append(allVMs, vmNAT1, vmNAT2)
	h.CleanupVMs(t, allVMs, out)

	h.WaitVMDone(t, vmNAT2, 300*time.Second)
	sw.Lap("NAT2 done (peer discovery + probes)")

	summary := map[string]interface{}{
		"topology":            "double_nat",
		"cluster_id":          clusterID,
		"mcast_port_internet": mcastPortInternet,
		"mcast_port_lan_a":    mcastPortLanA,
		"mcast_port_lan_b":    mcastPortLanB,
		"vm_disc_events":      vmDiscovery.GetEvents(),
		"vm_vps_events":       vmVPS.GetEvents(),
		"vm_router_a_events":  vmRouterA.GetEvents(),
		"vm_router_b_events":  vmRouterB.GetEvents(),
		"vm_nat1_events":      vmNAT1.GetEvents(),
		"vm_nat2_events":      vmNAT2.GetEvents(),
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))

	h.AssertProbes(t, vmNAT2.GetEvents(), "double_nat")
	sw.Lap("assertions")

	sw.Summary(out)
}
