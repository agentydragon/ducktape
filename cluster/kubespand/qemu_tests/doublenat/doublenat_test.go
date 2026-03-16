package doublenat_test

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestDoubleNAT(t *testing.T) {
	t.Parallel()
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

	tmpDir := t.TempDir()

	baseCfg := h.NewTestAgentConfig(clusterID, sharedSecret, discAddr)

	vmDiscovery := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastInternet, "52:54:00:ff:00:01"))
	sw.Lap("boot discovery VM")

	vpsCfg := baseCfg
	cidataVPS := h.CreateKubespandCIDATA(t, tmpDir, "vps", vpsCfg)
	vmVPS := h.BootVM(t, "vm-vps", vmlinuz, initramfs,
		"role=vps",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c0:00:01"), h.CIDATADrive(cidataVPS)...))
	sw.Lap("boot VPS VM")

	vmRouterA := h.BootVM(t, "vm-router-a", vmlinuz, initramfsRouter,
		"mode=router role=router-a internet_ip=192.168.50.1/24 lan_ip=192.168.60.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c1:00:01"),
			h.McastNIC("net1", mcastLanA, "52:54:00:c1:00:02")...))
	sw.Lap("boot Router-A VM")

	vmRouterB := h.BootVM(t, "vm-router-b", vmlinuz, initramfsRouter,
		"mode=router role=router-b internet_ip=192.168.50.3/24 lan_ip=192.168.70.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c2:00:01"),
			h.McastNIC("net1", mcastLanB, "52:54:00:c2:00:02")...))
	sw.Lap("boot Router-B VM")

	allVMs := []*h.VM{vmVPS, vmRouterA, vmRouterB, vmDiscovery}

	h.WaitForProbeServers(t, []*h.VM{vmDiscovery, vmRouterA, vmRouterB}, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	nat1Cfg := baseCfg
	cidataNAT1 := h.CreateKubespandCIDATA(t, tmpDir, "nat1", nat1Cfg)
	vmNAT1 := h.BootVM(t, "vm-nat1", vmlinuz, initramfs,
		"role=nat1",
		append(h.McastNIC("net0", mcastLanA, "52:54:00:d0:00:01"), h.CIDATADrive(cidataNAT1)...))
	sw.Lap("boot NAT1 VM")

	nat2Cfg := baseCfg
	nat2Cfg.Api.ListenTCP = ":50100"
	cidataNAT2 := h.CreateKubespandCIDATA(t, tmpDir, "nat2", nat2Cfg)
	vmNAT2 := h.BootVM(t, "vm-nat2", vmlinuz, initramfs,
		"role=nat2",
		append(h.McastNIC("net0", mcastLanB, "52:54:00:e0:00:01"), h.CIDATADrive(cidataNAT2)...),
		h.PortForward{GuestPort: h.COSIGuestPort})
	sw.Lap("boot NAT2 VM")

	allVMs = append(allVMs, vmNAT1, vmNAT2)
	h.CleanupVMs(t, allVMs, out)

	// Wait for all kubespand VMs to be ready (probe server responding).
	h.WaitForProbeServers(t, []*h.VM{vmVPS, vmNAT1, vmNAT2}, 180*time.Second)
	sw.Lap("kubespand VMs ready")

	// Poll NAT2's PeerStatus — in double-NAT, only the VPS peer is expected
	// to reach "up" (NAT1 is behind endpoint-dependent filtering).
	nat2Peers, err := vmNAT2.PollPeerStatus(1, 180*time.Second)
	if err != nil {
		t.Errorf("NAT2 peer status poll: %v", err)
	} else {
		t.Logf("NAT2 peers up: %v", nat2Peers)
	}
	sw.Lap("NAT2 peers up (host-side PeerStatus)")

	// Probe each discovered peer from NAT2.
	icmpOK := false
	tcpOK := false
	for _, peer := range nat2Peers {
		if vmNAT2.ProbeICMP(peer.Label, 60*time.Second) {
			icmpOK = true
		}
		if vmNAT2.ProbeTCP(peer.Label, 9999, 30*time.Second) {
			tcpOK = true
		}
	}

	// In double NAT, at least one peer (VPS) must be reachable.
	if !icmpOK {
		t.Errorf("no peer ULA ICMP probe succeeded (need at least VPS)")
	}
	if !tcpOK {
		t.Errorf("no peer ULA TCP probe succeeded (need at least VPS)")
	}
	sw.Lap("probes completed")

	// Dump diagnostics on failure.
	if t.Failed() {
		t.Log("=== NAT2 diagnostics ===")
		t.Log(vmNAT2.DumpDiagnostics())
		t.Log("=== VPS diagnostics ===")
		t.Log(vmVPS.DumpDiagnostics())
	}

	summary := map[string]interface{}{
		"topology":            "double_nat",
		"cluster_id":          clusterID,
		"mcast_port_internet": mcastPortInternet,
		"mcast_port_lan_a":    mcastPortLanA,
		"mcast_port_lan_b":    mcastPortLanB,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
	sw.Lap("assertions")

	sw.Summary(out)
}
