package kubespan_test

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestFlat(t *testing.T) {
	t.Parallel()
	runTopology(t, "flat")
}

func TestCrossSubnet(t *testing.T) {
	t.Parallel()
	runTopology(t, "cross_subnet")
}

func TestDiscoveryOnly(t *testing.T) {
	t.Parallel()
	runTopology(t, "discovery_only")
}

func runTopology(t *testing.T, topology string) {
	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfs := h.RunfilePath(t, h.KubespanInitramfs)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	out := h.OutputDir(t)

	clusterID := h.RandomBase64(32)
	sharedSecret := h.RandomBase64(32)
	mcastPort := h.RandomPort()

	var discIP string
	switch topology {
	case "flat", "discovery_only":
		discIP = "192.168.50.254"
	case "cross_subnet":
		discIP = "10.1.0.254"
	}
	discAddr := fmt.Sprintf("%s:3000", discIP)

	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	kernelBase := fmt.Sprintf("mode=kubespan cluster_id=%s shared_secret=%s discovery=%s topology=%s",
		clusterID, sharedSecret, discAddr, topology)

	t.Log("booting discovery VM...")
	vmDisc := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("mode=discovery role=discovery discovery_ip=%s/24 topology=%s", discIP, topology),
		h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01")...)

	t.Log("booting VM-B...")
	vmB := h.BootVM(t, "vm-b", vmlinuz, initramfs, kernelBase+" role=b",
		h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01")...)

	t.Log("booting VM-A...")
	vmA := h.BootVM(t, "vm-a", vmlinuz, initramfs, kernelBase+" role=a",
		h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01")...)

	allVMs := []*h.VM{vmA, vmB, vmDisc}
	h.CleanupVMs(t, allVMs, out)

	// Wait for discovery to be ready before expecting peer connections.
	h.RequireEvent(t, vmDisc, h.EventDone, 30*time.Second)

	h.WaitVMDone(t, vmA, 300*time.Second)

	summary := map[string]interface{}{
		"topology":       topology,
		"cluster_id":     clusterID,
		"mcast_port":     mcastPort,
		"vm_a_events":    vmA.GetEvents(),
		"vm_b_events":    vmB.GetEvents(),
		"vm_disc_events": vmDisc.GetEvents(),
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))

	h.AssertProbes(t, vmA.GetEvents(), topology)
}
