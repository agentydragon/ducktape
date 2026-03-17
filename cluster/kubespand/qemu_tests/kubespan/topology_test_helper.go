package kubespan_test

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	v1alpha1 "github.com/siderolabs/talos/pkg/machinery/config/types/v1alpha1"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func runTopology(t *testing.T, topology string, workerType h.WorkerType) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	out := h.OutputDir(t)
	sw.Lap("resolve runfiles")

	// Resolve kubespand initramfs only if needed.
	var kubespandInitramfs string
	if workerType == h.WorkerTypeKubespand {
		kubespandInitramfs = h.RunfilePath(t, h.KubespanInitramfs)
	}

	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	// Topology-specific parameters.
	var cpIP, discIP string
	var linkIPA, linkIPB, peerSubnetA, peerSubnetB string
	var cpRoutes []*v1alpha1.Route

	switch topology {
	case "flat":
		cpIP = "192.168.50.253"
		discIP = "192.168.50.254"
		linkIPA = "192.168.50.1"
		linkIPB = "192.168.50.2"
	case "cross_subnet":
		cpIP = "10.0.0.253"
		discIP = "10.1.0.254"
		linkIPA = "10.1.0.1"
		linkIPB = "10.2.0.1"
		peerSubnetA = "10.2.0.0/24,10.0.0.0/24"
		peerSubnetB = "10.1.0.0/24,10.0.0.0/24"
		cpRoutes = []*v1alpha1.Route{
			{RouteNetwork: "10.1.0.0/24"},
			{RouteNetwork: "10.2.0.0/24"},
		}
	}

	discAddr := fmt.Sprintf("%s:3000", discIP)
	tmpDir := t.TempDir()

	// Generate all crypto material and Talos configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	cpConfigData := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   fmt.Sprintf("%s/24", cpIP),
		Routes:               cpRoutes,
		ControlPlaneEndpoint: fmt.Sprintf("https://%s:6443", cpIP),
		DiscoveryEndpoint:    fmt.Sprintf("http://%s:3000", discIP),
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

	// Boot workers based on workerType.
	var workerA, workerB *h.WorkerNode
	var allVMs []*h.VM

	switch workerType {
	case h.WorkerTypeKubespand:
		cfgB := h.NewTestAgentConfig(creds, discAddr)
		cfgB.Kubespan.ListenPort = 51821
		cidataB := h.CreateKubespandCIDATA(t, tmpDir, "vm-b", cfgB)

		vmBArgs := fmt.Sprintf("role=vm-b link_ip=%s", linkIPB)
		if peerSubnetB != "" {
			vmBArgs += fmt.Sprintf(" peer_subnet=%s", peerSubnetB)
		}
		vmB := h.BootVM(t, "vm-b", vmlinuz, kubespandInitramfs,
			vmBArgs,
			append(h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"), h.CIDATADrive(cidataB)...))
		sw.Lap("boot VM-B")

		cfgA := h.NewTestAgentConfig(creds, discAddr)
		cfgA.Kubespan.ListenPort = 51820
		cfgA.Api.ListenTCP = ":50100"
		cidataA := h.CreateKubespandCIDATA(t, tmpDir, "vm-a", cfgA)

		vmAArgs := fmt.Sprintf("role=vm-a link_ip=%s", linkIPA)
		if peerSubnetA != "" {
			vmAArgs += fmt.Sprintf(" peer_subnet=%s", peerSubnetA)
		}
		vmA := h.BootVM(t, "vm-a", vmlinuz, kubespandInitramfs,
			vmAArgs,
			append(h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"), h.CIDATADrive(cidataA)...),
			h.PortForward{GuestPort: h.COSIGuestPort})
		sw.Lap("boot VM-A")

		workerA = h.NewKubespandWorker(t, vmA)
		workerB = h.NewKubespandWorker(t, vmB)
		allVMs = []*h.VM{vmA, vmB, vmDisc}

	case h.WorkerTypeTalos:
		workerAConfig := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   fmt.Sprintf("%s/24", linkIPA),
			ControlPlaneEndpoint: fmt.Sprintf("https://%s:6443", cpIP),
			DiscoveryEndpoint:    fmt.Sprintf("http://%s:3000", discIP),
		})
		workerBConfig := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   fmt.Sprintf("%s/24", linkIPB),
			ControlPlaneEndpoint: fmt.Sprintf("https://%s:6443", cpIP),
			DiscoveryEndpoint:    fmt.Sprintf("http://%s:3000", discIP),
		})
		aCI := h.CreateCIDATA(t, tmpDir, "vm-a", workerAConfig)
		bCI := h.CreateCIDATA(t, tmpDir, "vm-b", workerBConfig)

		aAPIPort := h.RandomPort()
		bAPIPort := h.RandomPort()
		vmA := h.BootTalosVM(t, "vm-a", talosBaseImage, aCI,
			aAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"))
		vmB := h.BootTalosVM(t, "vm-b", talosBaseImage, bCI,
			bAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"))
		sw.Lap("boot Talos workers")

		aClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", aAPIPort))
		bClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", bAPIPort))

		workerA = h.NewTalosWorker(t, vmA, aClient, linkIPA)
		workerB = h.NewTalosWorker(t, vmB, bClient, linkIPB)
		allVMs = []*h.VM{vmA, vmB, vmDisc}
	}
	defer workerA.Close()
	defer workerB.Close()

	// Talos CP VM — participates in KubeSpan mesh for API-based peer observation.
	cpCI := h.CreateCIDATA(t, tmpDir, "cp", cpConfigData)
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "vm-cp", talosBaseImage, cpCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:c0:00:01"))
	sw.Lap("boot Talos CP VM")

	allVMs = append(allVMs, vmCP)
	h.CleanupVMs(t, allVMs, out)

	// Discovery health check via HTTP from the test host.
	h.WaitForDiscoveryHTTP(t, vmDisc.ForwardAddr(3000), 120*time.Second)
	sw.Lap("discovery HTTP ready")

	// Wait for workers to be ready (parallel).
	h.WaitForWorkersReady(t, []*h.WorkerNode{workerA, workerB}, 180*time.Second)
	sw.Lap("workers ready")

	// Poll VM-A's PeerStatus via COSI — verify WireGuard handshake completes.
	vmAPeers, err := workerA.PollPeerStatus(1, 180*time.Second)
	if err != nil {
		t.Errorf("VM-A peer status poll: %v", err)
	} else {
		t.Logf("VM-A peers up: %v", vmAPeers)
	}
	sw.Lap("VM-A peers up")

	// Data-plane probes (kubespand workers only).
	if workerA.HasProbeServer() {
		peerSpecs, err := workerA.GetPeerSpecs()
		if err != nil {
			t.Errorf("VM-A GetPeerSpecs: %v", err)
		} else {
			t.Logf("VM-A peer specs: %v", peerSpecs)
		}

		var vmBULA string
		for _, ps := range peerSpecs {
			for _, ep := range ps.Endpoints {
				if ep.Addr().String() == linkIPB {
					vmBULA = ps.Address.String()
					break
				}
			}
			if vmBULA != "" {
				break
			}
		}

		peerBridgeIP := linkIPB

		if vmBULA != "" {
			if !workerA.ProbeICMP(vmBULA, 60*time.Second) {
				workerA.DumpDiagnostics(t)
			}
			if !workerA.ProbeTCP(vmBULA, 9999, 30*time.Second) {
				workerA.DumpDiagnostics(t)
			}
		} else {
			t.Error("could not determine VM-B's ULA, skipping ULA probes")
		}

		workerA.ProbeICMP(peerBridgeIP, 60*time.Second)
		workerA.ProbeTCP(peerBridgeIP, 9999, 30*time.Second)
		sw.Lap("probes completed")
	}

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

	// Dump diagnostics from workers on failure.
	if t.Failed() {
		workerA.DumpDiagnostics(t)
		workerB.DumpDiagnostics(t)
	}

	summary := map[string]interface{}{
		"topology":    topology,
		"worker_type": string(workerType),
		"cluster_id":  creds.ClusterID,
		"mcast_port":  mcastPort,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
	sw.Lap("assertions")

	sw.Summary(out)
}
