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
	for _, wt := range []h.NodeType{h.NodeTypeKubespand, h.NodeTypeTalos} {
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
func runDoubleNAT(t *testing.T, workerType h.NodeType) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	initramfsRouter := h.RunfilePath(t, h.RouterInitramfs)
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	kubespandInitramfs := h.RunfilePath(t, h.DoublenatInitramfs)
	sw.Lap("resolve runfiles")

	// Generate Talos secrets (VPS is always a Talos CP).
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	cpEndpoint := h.ControlPlaneEndpoint(h.DoubleNATVPSIP)
	discEndpoint := h.DiscoveryEndpoint(h.DoubleNATDiscoveryIP)

	vpsConfig := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   h.DoubleNATVPSIP + "/24",
		ControlPlaneEndpoint: cpEndpoint,
		DiscoveryEndpoint:    discEndpoint,
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
		vpsAPIPort, h.McastNIC("net0", mcastInternet, h.DoubleNATVPSMAC))
	sw.Lap("boot VPS CP")

	// Boot worker VMs (NAT1, NAT2) based on workerType.
	var nat1, nat2 *h.MeshNode
	allVMs := []*h.VM{vmVPS, vmRouterA, vmRouterB, vmDiscovery}

	switch workerType {
	case h.NodeTypeKubespand:
		nat1Cfg := h.NewTestAgentConfig(creds, h.DoubleNATDiscoveryAddr, cpEndpoint)
		cidataNAT1 := h.CreateKubespandCIDATA(t, tmpDir, "nat1", nat1Cfg)
		nat1APIPort := h.RandomPort()
		vmNAT1 := h.BootVM(t, "vm-nat1", vmlinuz, kubespandInitramfs,
			"role=nat1 link_ip="+h.DoubleNATNAT1IP+" default_gw="+h.DoubleNATNAT1Gateway,
			append(h.McastNIC("net0", mcastLanA, h.DoubleNATNAT1MAC), h.CIDATADrive(cidataNAT1)...),
			h.PortForward{GuestPort: h.COSIGuestPort},
			h.PortForward{HostPort: nat1APIPort, GuestPort: h.ApidGuestPort})

		nat2Cfg := h.NewTestAgentConfig(creds, h.DoubleNATDiscoveryAddr, cpEndpoint)
		cidataNAT2 := h.CreateKubespandCIDATA(t, tmpDir, "nat2", nat2Cfg)
		nat2APIPort := h.RandomPort()
		vmNAT2 := h.BootVM(t, "vm-nat2", vmlinuz, kubespandInitramfs,
			"role=nat2 link_ip="+h.DoubleNATNAT2IP+" default_gw="+h.DoubleNATNAT2Gateway,
			append(h.McastNIC("net0", mcastLanB, h.DoubleNATNAT2MAC), h.CIDATADrive(cidataNAT2)...),
			h.PortForward{GuestPort: h.COSIGuestPort},
			h.PortForward{HostPort: nat2APIPort, GuestPort: h.ApidGuestPort})

		nat1 = h.NewTalosMeshNode(t, vmNAT1, talosConfigPath, nat1APIPort)
		nat2 = h.NewTalosMeshNode(t, vmNAT2, talosConfigPath, nat2APIPort)
		allVMs = append(allVMs, vmNAT1, vmNAT2)

	case h.NodeTypeTalos:
		nat1Config := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   h.DoubleNATNAT1IP + "/24",
			Gateway:              h.DoubleNATNAT1Gateway,
			ControlPlaneEndpoint: cpEndpoint,
			DiscoveryEndpoint:    discEndpoint,
		})
		nat2Config := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   h.DoubleNATNAT2IP + "/24",
			Gateway:              h.DoubleNATNAT2Gateway,
			ControlPlaneEndpoint: cpEndpoint,
			DiscoveryEndpoint:    discEndpoint,
		})
		nat1CI := h.CreateCIDATA(t, tmpDir, "nat1", nat1Config)
		nat2CI := h.CreateCIDATA(t, tmpDir, "nat2", nat2Config)

		nat1APIPort := h.RandomPort()
		nat2APIPort := h.RandomPort()
		vmNAT1 := h.BootTalosVM(t, "vm-nat1", talosBaseImage, nat1CI,
			nat1APIPort, h.McastNIC("net0", mcastLanA, h.DoubleNATNAT1MAC))
		vmNAT2 := h.BootTalosVM(t, "vm-nat2", talosBaseImage, nat2CI,
			nat2APIPort, h.McastNIC("net0", mcastLanB, h.DoubleNATNAT2MAC))

		nat1 = h.NewTalosMeshNode(t, vmNAT1, talosConfigPath, nat1APIPort)
		nat2 = h.NewTalosMeshNode(t, vmNAT2, talosConfigPath, nat2APIPort)
		allVMs = append(allVMs, vmNAT1, vmNAT2)
	}
	sw.Lap("boot worker VMs")

	h.CleanupVMs(t, allVMs, out)

	// Wait for infrastructure.
	h.WaitForProbeServers(t, []*h.VM{vmDiscovery, vmRouterA, vmRouterB}, 120*time.Second)
	sw.Lap("infrastructure VMs ready")

	vpsNode := h.NewTalosMeshNode(t, vmVPS, talosConfigPath, vpsAPIPort)

	// Run convergence loop: watches COSI on all nodes, streams dmesg,
	// fires probes when peers come up, exits on full mesh + probes pass.
	runner := &h.MeshTestRunner{
		T:            t,
		Nodes:        []*h.MeshNode{vpsNode, nat1, nat2},
		Stopwatch:    sw,
		OutDir:       out,
		SuccessFunc:  h.FullMeshSuccess(2), // each node expects 2 peers
		ProbeTargets: h.ULAProbeTargets,
	}
	runner.Run(t.Context())

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
}
