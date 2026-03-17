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

	creds := h.NewRandomCreds()

	mcastPortInternet := h.RandomPort()
	mcastPortLanA := h.RandomPort()
	mcastPortLanB := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLanA := fmt.Sprintf("230.0.0.1:%d", mcastPortLanA)
	mcastLanB := fmt.Sprintf("230.0.0.1:%d", mcastPortLanB)

	tmpDir := t.TempDir()

	baseCfg := h.NewTestAgentConfig(creds, h.DoubleNATDiscoveryAddr)

	vmDiscovery := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		"role=discovery discovery_ip="+h.DoubleNATDiscoveryCIDR,
		h.McastNIC("net0", mcastInternet, h.DoubleNATDiscoveryMAC))
	sw.Lap("boot discovery VM")

	vpsCfg := baseCfg
	cidataVPS := h.CreateKubespandCIDATA(t, tmpDir, "vps", vpsCfg)
	vmVPS := h.BootVM(t, "vm-vps", vmlinuz, initramfs,
		"role=vps link_ip="+h.DoubleNATVPSIP,
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c0:00:01"), h.CIDATADrive(cidataVPS)...))
	sw.Lap("boot VPS VM")

	vmRouterA := h.BootVM(t, "vm-router-a", vmlinuz, initramfsRouter,
		"role=router-a internet_ip="+h.DoubleNATRouterAInternetCIDR+" lan_ip="+h.DoubleNATRouterALanCIDR,
		append(h.McastNIC("net0", mcastInternet, h.DoubleNATRouterAInternetMAC),
			h.McastNIC("net1", mcastLanA, h.DoubleNATRouterALanMAC)...))
	sw.Lap("boot Router-A VM")

	vmRouterB := h.BootVM(t, "vm-router-b", vmlinuz, initramfsRouter,
		"role=router-b internet_ip="+h.DoubleNATRouterBInternetCIDR+" lan_ip="+h.DoubleNATRouterBLanCIDR,
		append(h.McastNIC("net0", mcastInternet, h.DoubleNATRouterBInternetMAC),
			h.McastNIC("net1", mcastLanB, h.DoubleNATRouterBLanMAC)...))
	sw.Lap("boot Router-B VM")

	allVMs := []*h.VM{vmVPS, vmRouterA, vmRouterB, vmDiscovery}

	h.WaitForProbeServers(t, []*h.VM{vmDiscovery, vmRouterA, vmRouterB}, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	nat1Cfg := baseCfg
	cidataNAT1 := h.CreateKubespandCIDATA(t, tmpDir, "nat1", nat1Cfg)
	vmNAT1 := h.BootVM(t, "vm-nat1", vmlinuz, initramfs,
		"role=nat1 link_ip="+h.DoubleNATNAT1IP+" default_gw="+h.DoubleNATNAT1Gateway,
		append(h.McastNIC("net0", mcastLanA, "52:54:00:d0:00:01"), h.CIDATADrive(cidataNAT1)...))
	sw.Lap("boot NAT1 VM")

	nat2Cfg := baseCfg
	nat2Cfg.Api.ListenTCP = ":50100"
	cidataNAT2 := h.CreateKubespandCIDATA(t, tmpDir, "nat2", nat2Cfg)
	vmNAT2 := h.BootVM(t, "vm-nat2", vmlinuz, initramfs,
		"role=nat2 link_ip="+h.DoubleNATNAT2IP+" default_gw="+h.DoubleNATNAT2Gateway,
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

	// Get peer ULAs from PeerSpec (PeerStatusSpec.Label is hostname, not address).
	peerSpecs, err := vmNAT2.GetPeerSpecs()
	if err != nil {
		t.Errorf("NAT2 GetPeerSpecs: %v", err)
	} else {
		t.Logf("NAT2 peer specs: %v", peerSpecs)
	}

	// Probe each discovered peer from NAT2.
	icmpOK := false
	tcpOK := false
	for _, ps := range peerSpecs {
		addr := ps.Address.String()
		if vmNAT2.ProbeICMP(addr, 60*time.Second) {
			icmpOK = true
		}
		if vmNAT2.ProbeTCP(addr, 9999, 30*time.Second) {
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
		"cluster_id":          creds.ClusterID,
		"mcast_port_internet": mcastPortInternet,
		"mcast_port_lan_a":    mcastPortLanA,
		"mcast_port_lan_b":    mcastPortLanB,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
	sw.Lap("assertions")

	sw.Summary(out)
}
