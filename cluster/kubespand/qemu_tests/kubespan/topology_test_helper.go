package kubespan_test

import (
	"encoding/json"
	"fmt"
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

	// Choose CP config and IP based on topology.
	var cpConfigPath, cpIP string
	switch topology {
	case "flat":
		cpConfigPath = h.KubespanCPConfig
		cpIP = "192.168.50.253"
	case "cross_subnet":
		cpConfigPath = h.KubespanCPCrossConfig
		cpIP = "10.0.0.253"
	}

	cpConfigData := h.ReadRunfile(t, cpConfigPath)
	cpCfg := h.ParseTalosConfig(t, cpConfigData)
	clusterID := cpCfg.ClusterConfig.ClusterID
	sharedSecret := cpCfg.ClusterConfig.ClusterSecret

	var discIP string
	switch topology {
	case "flat":
		discIP = "192.168.50.254"
	case "cross_subnet":
		discIP = "10.1.0.254"
	}
	discAddr := fmt.Sprintf("%s:3000", discIP)

	// Discovery VM with extra forward for HTTP health check from the test host.
	vmDisc := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("mode=discovery role=discovery discovery_ip=%s/24 topology=%s", discIP, topology),
		h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01"),
		h.PortForward{GuestPort: 3000})
	sw.Lap("boot discovery VM")

	tmpDir := t.TempDir()

	// Build kubespand agent configs per role and topology.
	var endpointFiltersA, endpointFiltersB []string
	var listenPortA, listenPortB int
	switch topology {
	case "flat":
		endpointFiltersA = []string{"192.168.50.0/24"}
		endpointFiltersB = []string{"192.168.50.0/24"}
		listenPortA = 51820
		listenPortB = 51821
	case "cross_subnet":
		endpointFiltersA = []string{"10.0.0.0/8"}
		endpointFiltersB = []string{"10.0.0.0/8"}
		listenPortA = 51820
		listenPortB = 51821
	}

	cfgB := h.NewTestAgentConfig(clusterID, sharedSecret, discAddr)
	cfgB.Kubespan.ListenPort = listenPortB
	cfgB.Kubespan.EndpointFilters = endpointFiltersB
	cidataB := h.CreateKubespandCIDATA(t, tmpDir, "vm-b", cfgB)

	vmB := h.BootVM(t, "vm-b", vmlinuz, initramfs,
		fmt.Sprintf("role=b topology=%s", topology),
		append(h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"), h.CIDATADrive(cidataB)...))
	sw.Lap("boot VM-B")

	// Talos CP VM — participates in KubeSpan mesh for API-based peer observation.
	cpCI := h.CreateCIDATA(t, tmpDir, "cp", cpConfigData)
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "vm-cp", talosBaseImage, cpCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:c0:00:01"))
	sw.Lap("boot Talos CP VM")

	cfgA := h.NewTestAgentConfig(clusterID, sharedSecret, discAddr)
	cfgA.Kubespan.ListenPort = listenPortA
	cfgA.Kubespan.EndpointFilters = endpointFiltersA
	cfgA.Api.ListenTCP = ":50100"
	cidataA := h.CreateKubespandCIDATA(t, tmpDir, "vm-a", cfgA)

	vmA := h.BootVM(t, "vm-a", vmlinuz, initramfs,
		fmt.Sprintf("role=a topology=%s", topology),
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

	// Determine peer bridge IP based on topology and role.
	var peerBridgeIP string
	switch topology {
	case "flat":
		peerBridgeIP = "192.168.50.2" // VM-B's eth0 IP
	case "cross_subnet":
		peerBridgeIP = "10.2.0.1" // VM-B's eth0 IP
	}

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
	talosClient := h.NewTalosClient(t, h.RunfilePath(t, h.TalosConfig), fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
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
		"cluster_id": clusterID,
		"mcast_port": mcastPort,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
	sw.Lap("assertions")

	sw.Summary(out)
}
