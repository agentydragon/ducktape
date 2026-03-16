package talos_test

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestTalosKubeSpanDoubleNAT(t *testing.T) {
	t.Parallel()
	sw := h.NewStopwatch(t)

	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	alpineVmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	alpineInitramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	alpineInitramfsRouter := h.RunfilePath(t, h.RouterInitramfs)
	sw.Lap("resolve runfiles")

	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	// Generate all crypto material and Talos configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)

	vpsConfig := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   "192.168.50.2/24",
		ControlPlaneEndpoint: "https://192.168.50.2:6443",
		DiscoveryEndpoint:    "http://192.168.50.254:3000",
		EndpointFilters:      []string{"0.0.0.0/0"},
		CertSANs:             []string{"192.168.50.2", "127.0.0.1"},
	})
	nat1Config := secrets.WorkerConfig(h.TalosNodeConfig{
		IP:                   "192.168.60.2/24",
		Gateway:              "192.168.60.1",
		ControlPlaneEndpoint: "https://192.168.50.2:6443",
		DiscoveryEndpoint:    "http://192.168.50.254:3000",
		EndpointFilters:      []string{"0.0.0.0/0"},
	})
	nat2Config := secrets.WorkerConfig(h.TalosNodeConfig{
		IP:                   "192.168.70.2/24",
		Gateway:              "192.168.70.1",
		ControlPlaneEndpoint: "https://192.168.50.2:6443",
		DiscoveryEndpoint:    "http://192.168.50.254:3000",
		EndpointFilters:      []string{"0.0.0.0/0"},
	})
	talosConfigPath := secrets.WriteTalosconfig(t, tmpDir)

	vpsCI := h.CreateCIDATA(t, tmpDir, "vps", vpsConfig)
	nat1CI := h.CreateCIDATA(t, tmpDir, "nat1", nat1Config)
	nat2CI := h.CreateCIDATA(t, tmpDir, "nat2", nat2Config)
	sw.Lap("generate configs + create CIDATA volumes")

	mcastPortInternet := h.RandomPort()
	mcastPortLan1 := h.RandomPort()
	mcastPortLan2 := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLan1 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan1)
	mcastLan2 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan2)

	vmDiscovery := h.BootVM(t, "talos-disc", alpineVmlinuz, alpineInitramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastInternet, "52:54:00:ff:00:01"))
	vmRouter1 := h.BootVM(t, "talos-router-1", alpineVmlinuz, alpineInitramfsRouter,
		"mode=router role=router-1 internet_ip=192.168.50.1/24 lan_ip=192.168.60.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c1:00:01"),
			h.McastNIC("net1", mcastLan1, "52:54:00:c1:00:02")...))
	vmRouter2 := h.BootVM(t, "talos-router-2", alpineVmlinuz, alpineInitramfsRouter,
		"mode=router role=router-2 internet_ip=192.168.50.3/24 lan_ip=192.168.70.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c2:00:01"),
			h.McastNIC("net1", mcastLan2, "52:54:00:c2:00:02")...))
	sw.Lap("boot infrastructure VMs (discovery + routers)")

	vpsAPIPort := h.RandomPort()
	nat1APIPort := h.RandomPort()
	nat2APIPort := h.RandomPort()

	vmVPS := h.BootTalosVM(t, "talos-vps", talosBaseImage, vpsCI,
		vpsAPIPort, h.McastNIC("net0", mcastInternet, "52:54:00:a0:00:01"))
	vmNAT1 := h.BootTalosVM(t, "talos-nat1", talosBaseImage, nat1CI,
		nat1APIPort, h.McastNIC("net0", mcastLan1, "52:54:00:a0:00:02"))
	vmNAT2 := h.BootTalosVM(t, "talos-nat2", talosBaseImage, nat2CI,
		nat2APIPort, h.McastNIC("net0", mcastLan2, "52:54:00:a0:00:03"))
	sw.Lap("boot Talos VMs")

	h.WaitForProbeServers(t, []*h.VM{vmDiscovery, vmRouter1, vmRouter2}, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	allVMs := []*h.VM{vmVPS, vmNAT1, vmNAT2, vmRouter1, vmRouter2, vmDiscovery}
	h.CleanupVMs(t, allVMs, out)

	// Create Talos API clients for all three nodes.
	type talosNode struct {
		name     string
		endpoint string
		nodeIP   string
	}
	nodes := []talosNode{
		{"vps", fmt.Sprintf("127.0.0.1:%d", vpsAPIPort), "192.168.50.2"},
		{"nat1", fmt.Sprintf("127.0.0.1:%d", nat1APIPort), "192.168.60.2"},
		{"nat2", fmt.Sprintf("127.0.0.1:%d", nat2APIPort), "192.168.70.2"},
	}

	// Wait for Talos API on all nodes.
	// Observed on RBE (Firecracker, TCG): apid healthy ~64s after VM start.
	for _, n := range nodes {
		c := h.NewTalosClient(t, talosConfigPath, n.endpoint)
		defer c.Close()
		h.WaitForTalosAPI(t, c, n.nodeIP, 120*time.Second)
		t.Logf("Talos API ready on %s (%s)", n.name, n.nodeIP)
	}
	sw.Lap("Talos API ready (all nodes)")

	// Poll KubeSpan status from VPS (controlplane, sees all peers).
	vpsClient := h.NewTalosClient(t, talosConfigPath, nodes[0].endpoint)
	defer vpsClient.Close()

	peers, err := h.PollKubeSpanStatus(t, vpsClient, nodes[0].nodeIP, 300*time.Second)
	sw.Lap("KubeSpan status poll (VPS)")

	statusJSON, _ := json.MarshalIndent(peers, "", "  ")
	h.SaveArtifact(t, out, "kubespan-status-vps.json", string(statusJSON))

	if err != nil {
		t.Errorf("KubeSpan peer discovery failed (VPS): %v", err)
	}
	for _, peer := range peers {
		if peer.State != kubespan.PeerStateUp {
			t.Errorf("VPS peer %s state=%s (want up), endpoint=%s", peer.Label, peer.State, peer.Endpoint)
		}
	}
	if len(peers) < 2 {
		t.Errorf("VPS: expected 2 KubeSpan peers, got %d", len(peers))
	}

	// Also dump KubeSpan diagnostics from NAT1 and NAT2 for symmetry.
	for _, n := range nodes[1:] {
		c := h.NewTalosClient(t, talosConfigPath, n.endpoint)
		defer c.Close()
		h.DumpKubeSpanDiagnostics(t, c, n.nodeIP)
	}
	sw.Lap("diagnostics (NAT1, NAT2)")

	sw.Summary(out)
}
