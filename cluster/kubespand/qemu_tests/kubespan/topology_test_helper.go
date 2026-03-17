package kubespan_test

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func runTopology(t *testing.T, topology string) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfs := h.RunfilePath(t, h.KubespanInitramfs)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	out := h.OutputDir(t)
	sw.Lap("resolve runfiles")

	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	// Topology-specific parameters.
	var cpIP, discIP string
	var linkIPA, linkIPB, peerSubnetA, peerSubnetB string
	var endpointFilters []string

	switch topology {
	case "flat":
		cpIP = "192.168.50.253"
		discIP = "192.168.50.254"
		linkIPA = "192.168.50.1"
		linkIPB = "192.168.50.2"
		endpointFilters = []string{"192.168.50.0/24"}
	case "cross_subnet":
		cpIP = "10.0.0.253"
		discIP = "10.1.0.254"
		linkIPA = "10.1.0.1"
		linkIPB = "10.2.0.1"
		peerSubnetA = "10.2.0.0/24"
		peerSubnetB = "10.1.0.0/24"
		endpointFilters = []string{"10.0.0.0/8"}
	}

	discAddr := fmt.Sprintf("%s:3000", discIP)
	tmpDir := t.TempDir()

	// Generate all crypto material and Talos configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	cpConfigData := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   fmt.Sprintf("%s/24", cpIP),
		ControlPlaneEndpoint: fmt.Sprintf("https://%s:6443", cpIP),
		DiscoveryEndpoint:    fmt.Sprintf("http://%s:3000", discIP),
		EndpointFilters:      endpointFilters,
		CertSANs:             []string{cpIP, "127.0.0.1"},
	})

	talosConfigPath := filepath.Join(tmpDir, "talosconfig")
	secrets.WriteTalosconfig(t, talosConfigPath)
	sw.Lap("generate talos configs")

	// Discovery VM with extra forward for HTTP health check from the test host.
	vmDisc := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("role=discovery discovery_ip=%s/24 topology=%s", discIP, topology),
		h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01"),
		h.PortForward{GuestPort: 3000})
	sw.Lap("boot discovery VM")

	cfgB := h.NewTestAgentConfig(creds, discAddr)
	cfgB.Kubespan.ListenPort = 51821
	cfgB.Kubespan.EndpointFilters = endpointFilters
	cidataB := h.CreateKubespandCIDATA(t, tmpDir, "vm-b", cfgB)

	vmBArgs := fmt.Sprintf("role=vm-b link_ip=%s", linkIPB)
	if peerSubnetB != "" {
		vmBArgs += fmt.Sprintf(" peer_subnet=%s", peerSubnetB)
	}
	vmB := h.BootVM(t, "vm-b", vmlinuz, initramfs,
		vmBArgs,
		append(h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"), h.CIDATADrive(cidataB)...))
	sw.Lap("boot VM-B")

	// Talos CP VM — participates in KubeSpan mesh for API-based peer observation.
	cpCI := h.CreateCIDATA(t, tmpDir, "cp", cpConfigData)
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "vm-cp", talosBaseImage, cpCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:c0:00:01"))
	sw.Lap("boot Talos CP VM")

	cfgA := h.NewTestAgentConfig(creds, discAddr)
	cfgA.Kubespan.ListenPort = 51820
	cfgA.Kubespan.EndpointFilters = endpointFilters
	cfgA.Api.ListenTCP = ":50100"
	cidataA := h.CreateKubespandCIDATA(t, tmpDir, "vm-a", cfgA)

	vmAArgs := fmt.Sprintf("role=vm-a link_ip=%s", linkIPA)
	if peerSubnetA != "" {
		vmAArgs += fmt.Sprintf(" peer_subnet=%s", peerSubnetA)
	}
	vmA := h.BootVM(t, "vm-a", vmlinuz, initramfs,
		vmAArgs,
		append(h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"), h.CIDATADrive(cidataA)...),
		h.PortForward{GuestPort: h.COSIGuestPort})
	sw.Lap("boot VM-A")

	allVMs := []*h.VM{vmA, vmB, vmDisc, vmCP}
	h.CleanupVMs(t, allVMs, out)

	// Discovery health check via HTTP from the test host.
	h.WaitForDiscoveryHTTP(t, vmDisc.ForwardAddr(3000), 120*time.Second)
	sw.Lap("discovery HTTP ready")

	// Wait for both Alpine VMs to be ready (probe server responding).
	h.WaitForProbeServers(t, []*h.VM{vmA, vmB}, 180*time.Second)
	sw.Lap("VMs ready")

	// Poll VM-A's PeerStatus via COSI — verify WireGuard handshake completes.
	vmAPeers, err := vmA.PollPeerStatus(1, 180*time.Second)
	if err != nil {
		t.Errorf("VM-A peer status poll: %v", err)
	} else {
		t.Logf("VM-A peers up: %v", vmAPeers)
	}
	sw.Lap("VM-A peers up (host-side PeerStatus)")

	// Get peer ULA from the PeerStatus results.
	var peerULA string
	if len(vmAPeers) > 0 {
		peerULA = vmAPeers[0].Label
	}

	// VM-B's eth0 IP — use the same linkIPB we configured above.
	peerBridgeIP := linkIPB

	// Run probes from VM-A to VM-B.
	if peerULA != "" {
		if !vmA.ProbeICMP(peerULA, 60*time.Second) {
			t.Log(vmA.DumpDiagnostics())
		}
		if !vmA.ProbeTCP(peerULA, 9999, 30*time.Second) {
			t.Log(vmA.DumpDiagnostics())
		}
	} else {
		t.Error("no peer ULA discovered, skipping ULA probes")
	}

	vmA.ProbeICMP(peerBridgeIP, 60*time.Second)
	vmA.ProbeTCP(peerBridgeIP, 9999, 30*time.Second)
	sw.Lap("probes completed")

	// Observe KubeSpan peer status from the Talos CP's API.
	talosClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
	defer talosClient.Close()
	h.WaitForTalosAPI(t, talosClient, cpIP, 180*time.Second)
	sw.Lap("Talos CP API ready")

	h.DumpKubeSpanDiagnostics(t, talosClient, cpIP)
	sw.Lap("initial diagnostics")

	peers, err := h.PollKubeSpanStatus(t, talosClient, cpIP, 120*time.Second)
	if err != nil {
		h.DumpKubeSpanDiagnostics(t, talosClient, cpIP)
		t.Errorf("peer discovery via Talos API: %v", err)
	} else {
		t.Logf("KubeSpan peers observed from Talos CP: %v", peers)
	}
	sw.Lap("peer discovery via Talos API")

	// Dump diagnostics from both VMs on failure.
	if t.Failed() {
		t.Log("=== VM-A diagnostics ===")
		t.Log(vmA.DumpDiagnostics())
		t.Log("=== VM-B diagnostics ===")
		t.Log(vmB.DumpDiagnostics())
	}

	summary := map[string]interface{}{
		"topology":   topology,
		"cluster_id": creds.ClusterID,
		"mcast_port": mcastPort,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
	sw.Lap("assertions")

	sw.Summary(out)
}
