package doublenat_test

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestDoubleNAT(t *testing.T) {
	t.Parallel()
	for _, wt := range []h.WorkerType{h.WorkerTypeKubespand, h.WorkerTypeTalos} {
		t.Run(string(wt), func(t *testing.T) {
			t.Parallel()
			runDoubleNAT(t, wt)
		})
	}
}

// runDoubleNAT tests KubeSpan connectivity across a double-NAT topology.
// VPS is always a Talos controlplane. NAT1 and NAT2 workers are parameterized
// by workerType (Talos or kubespand).
//
// Topology:
//
//	[NAT1] --[LAN-A]-- [Router-A] --+
//	                                 |
//	                            [Internet]
//	                                 |
//	[NAT2] --[LAN-B]-- [Router-B] --+
//	                                 |
//	                            [VPS (CP)]
//	                          [Discovery]
func runDoubleNAT(t *testing.T, workerType h.WorkerType) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	initramfsRouter := h.RunfilePath(t, h.RouterInitramfs)
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	// Resolve worker-specific runfiles.
	var kubespandInitramfs string
	if workerType == h.WorkerTypeKubespand {
		kubespandInitramfs = h.RunfilePath(t, h.DoublenatInitramfs)
	}
	sw.Lap("resolve runfiles")

	// Generate Talos secrets (VPS is always a Talos CP).
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	vpsConfig := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   h.DoubleNATVPSIP + "/24",
		ControlPlaneEndpoint: "https://" + h.DoubleNATVPSIP + ":6443",
		DiscoveryEndpoint:    "http://" + h.DoubleNATDiscoveryAddr,
		CertSANs:             []string{h.DoubleNATVPSIP, "127.0.0.1"},
	})
	talosConfigPath := filepath.Join(tmpDir, "talosconfig")
	secrets.WriteTalosconfig(t, talosConfigPath)

	vpsCI := h.CreateCIDATA(t, tmpDir, "vps", vpsConfig)
	sw.Lap("generate configs")

	// Network segments.
	mcastPortInternet := h.RandomPort()
	mcastPortLanA := h.RandomPort()
	mcastPortLanB := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLanA := fmt.Sprintf("230.0.0.1:%d", mcastPortLanA)
	mcastLanB := fmt.Sprintf("230.0.0.1:%d", mcastPortLanB)

	// Infrastructure VMs (always Alpine).
	vmDiscovery := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		"role=discovery discovery_ip="+h.DoubleNATDiscoveryCIDR,
		h.McastNIC("net0", mcastInternet, h.DoubleNATDiscoveryMAC))
	vmRouterA := h.BootVM(t, "vm-router-a", vmlinuz, initramfsRouter,
		"role=router-a internet_ip="+h.DoubleNATRouterAInternetCIDR+" lan_ip="+h.DoubleNATRouterALanCIDR,
		append(h.McastNIC("net0", mcastInternet, h.DoubleNATRouterAInternetMAC),
			h.McastNIC("net1", mcastLanA, h.DoubleNATRouterALanMAC)...))
	vmRouterB := h.BootVM(t, "vm-router-b", vmlinuz, initramfsRouter,
		"role=router-b internet_ip="+h.DoubleNATRouterBInternetCIDR+" lan_ip="+h.DoubleNATRouterBLanCIDR,
		append(h.McastNIC("net0", mcastInternet, h.DoubleNATRouterBInternetMAC),
			h.McastNIC("net1", mcastLanB, h.DoubleNATRouterBLanMAC)...))
	sw.Lap("boot infrastructure VMs")

	// VPS (Talos CP).
	vpsAPIPort := h.RandomPort()
	vmVPS := h.BootTalosVM(t, "vm-vps", talosBaseImage, vpsCI,
		vpsAPIPort, h.McastNIC("net0", mcastInternet, "52:54:00:a0:00:01"))
	sw.Lap("boot VPS CP")

	// Boot worker VMs (NAT1, NAT2) based on workerType.
	var nat1, nat2 *h.WorkerNode
	allVMs := []*h.VM{vmVPS, vmRouterA, vmRouterB, vmDiscovery}

	switch workerType {
	case h.WorkerTypeKubespand:
		nat1Cfg := h.NewTestAgentConfig(creds, h.DoubleNATDiscoveryAddr)
		cidataNAT1 := h.CreateKubespandCIDATA(t, tmpDir, "nat1", nat1Cfg)
		vmNAT1 := h.BootVM(t, "vm-nat1", vmlinuz, kubespandInitramfs,
			"role=nat1 link_ip="+h.DoubleNATNAT1IP+" default_gw="+h.DoubleNATNAT1Gateway,
			append(h.McastNIC("net0", mcastLanA, "52:54:00:d0:00:01"), h.CIDATADrive(cidataNAT1)...))

		nat2Cfg := h.NewTestAgentConfig(creds, h.DoubleNATDiscoveryAddr)
		nat2Cfg.Api.ListenTCP = ":50100"
		cidataNAT2 := h.CreateKubespandCIDATA(t, tmpDir, "nat2", nat2Cfg)
		vmNAT2 := h.BootVM(t, "vm-nat2", vmlinuz, kubespandInitramfs,
			"role=nat2 link_ip="+h.DoubleNATNAT2IP+" default_gw="+h.DoubleNATNAT2Gateway,
			append(h.McastNIC("net0", mcastLanB, "52:54:00:e0:00:01"), h.CIDATADrive(cidataNAT2)...),
			h.PortForward{GuestPort: h.COSIGuestPort})

		nat1 = h.NewKubespandWorker(t, vmNAT1)
		nat2 = h.NewKubespandWorker(t, vmNAT2)
		allVMs = append(allVMs, vmNAT1, vmNAT2)

	case h.WorkerTypeTalos:
		nat1Config := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   h.DoubleNATNAT1IP + "/24",
			Gateway:              h.DoubleNATNAT1Gateway,
			ControlPlaneEndpoint: "https://" + h.DoubleNATVPSIP + ":6443",
			DiscoveryEndpoint:    "http://" + h.DoubleNATDiscoveryAddr,
		})
		nat2Config := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   h.DoubleNATNAT2IP + "/24",
			Gateway:              h.DoubleNATNAT2Gateway,
			ControlPlaneEndpoint: "https://" + h.DoubleNATVPSIP + ":6443",
			DiscoveryEndpoint:    "http://" + h.DoubleNATDiscoveryAddr,
		})
		nat1CI := h.CreateCIDATA(t, tmpDir, "nat1", nat1Config)
		nat2CI := h.CreateCIDATA(t, tmpDir, "nat2", nat2Config)

		nat1APIPort := h.RandomPort()
		nat2APIPort := h.RandomPort()
		vmNAT1 := h.BootTalosVM(t, "vm-nat1", talosBaseImage, nat1CI,
			nat1APIPort, h.McastNIC("net0", mcastLanA, "52:54:00:d0:00:01"))
		vmNAT2 := h.BootTalosVM(t, "vm-nat2", talosBaseImage, nat2CI,
			nat2APIPort, h.McastNIC("net0", mcastLanB, "52:54:00:e0:00:01"))

		nat1Client := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", nat1APIPort))
		nat2Client := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", nat2APIPort))

		nat1 = h.NewTalosWorker(t, vmNAT1, nat1Client, h.DoubleNATNAT1IP)
		nat2 = h.NewTalosWorker(t, vmNAT2, nat2Client, h.DoubleNATNAT2IP)
		allVMs = append(allVMs, vmNAT1, vmNAT2)
	}
	defer nat1.Close()
	defer nat2.Close()
	sw.Lap("boot worker VMs")

	h.CleanupVMs(t, allVMs, out)

	// Wait for infrastructure.
	h.WaitForProbeServers(t, []*h.VM{vmDiscovery, vmRouterA, vmRouterB}, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	// Wait for VPS Talos API.
	vpsClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", vpsAPIPort))
	defer vpsClient.Close()
	h.WaitForTalosAPI(t, vpsClient, h.DoubleNATVPSIP, 120*time.Second)
	sw.Lap("VPS Talos API ready")

	// Wait for workers to be ready (parallel).
	h.WaitForWorkersReady(t, []*h.WorkerNode{nat1, nat2}, 180*time.Second)
	sw.Lap("workers ready")

	// Poll VPS PeerStatus — should see 2 worker peers "up".
	vpsPeers, err := h.PollKubeSpanStatus(t, vpsClient, h.DoubleNATVPSIP, 300*time.Second)
	if err != nil {
		t.Errorf("VPS peer status poll: %v", err)
	} else {
		t.Logf("VPS peers: %v", vpsPeers)
	}
	sw.Lap("VPS peers up")

	// Poll NAT2 PeerStatus — at least 1 peer (VPS) should be "up".
	nat2Peers, err := nat2.PollPeerStatus(1, 300*time.Second)
	if err != nil {
		t.Errorf("NAT2 peer status poll: %v", err)
	} else {
		t.Logf("NAT2 peers: %v", nat2Peers)
	}
	sw.Lap("NAT2 peers up")

	// Data-plane probes (kubespand workers only).
	if nat2.HasProbeServer() {
		peerSpecs, err := nat2.GetPeerSpecs()
		if err != nil {
			t.Errorf("NAT2 GetPeerSpecs: %v", err)
		} else {
			t.Logf("NAT2 peer specs: %v", peerSpecs)
		}

		icmpOK := false
		tcpOK := false
		for _, ps := range peerSpecs {
			addr := ps.Address.String()
			if nat2.ProbeICMP(addr, 60*time.Second) {
				icmpOK = true
			}
			if nat2.ProbeTCP(addr, 9999, 30*time.Second) {
				tcpOK = true
			}
		}
		if !icmpOK {
			t.Errorf("no peer ULA ICMP probe succeeded (need at least VPS)")
		}
		if !tcpOK {
			t.Errorf("no peer ULA TCP probe succeeded (need at least VPS)")
		}
		sw.Lap("probes completed")
	}

	// Dump diagnostics on failure.
	if t.Failed() {
		nat1.DumpDiagnostics(t)
		nat2.DumpDiagnostics(t)
		h.DumpKubeSpanDiagnostics(t, vpsClient, h.DoubleNATVPSIP)
	}

	summary := map[string]interface{}{
		"topology":            "double_nat",
		"worker_type":         string(workerType),
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
