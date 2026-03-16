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
	// flat: CP at 192.168.50.253 on the shared flat segment.
	// cross_subnet: CP at 10.0.0.253/8 ("public internet" reachable from both subnets).
	var cpConfigPath, cpIP string
	switch topology {
	case "flat":
		cpConfigPath = h.KubespanCPConfig
		cpIP = "192.168.50.253"
	case "cross_subnet":
		cpConfigPath = h.KubespanCPCrossConfig
		cpIP = "10.0.0.253"
	}

	// Use CP config credentials so all participants share the same
	// cluster ID/secret and discover each other.
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

	// Discovery VM with mgmt NIC for HTTP health check from the test host.
	discHTTPPort := h.RandomPort()
	vmDisc := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("mode=discovery role=discovery discovery_ip=%s/24 topology=%s", discIP, topology),
		append(h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01"),
			h.MgmtNIC(discHTTPPort, 3000, "52:54:00:ff:00:02")...)...)
	sw.Lap("boot discovery VM")

	kernelBase := fmt.Sprintf("mode=kubespan cluster_id=%s shared_secret=%s discovery=%s topology=%s",
		clusterID, sharedSecret, discAddr, topology)

	vmB := h.BootVM(t, "vm-b", vmlinuz, initramfs, kernelBase+" role=b",
		h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01")...)
	sw.Lap("boot VM-B")

	// Talos CP VM — participates in KubeSpan mesh for API-based peer observation.
	tmpDir := t.TempDir()
	cpCI := h.CreateCIDATA(t, tmpDir, "cp", cpConfigData)
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "vm-cp", talosBaseImage, cpCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:c0:00:01"))
	sw.Lap("boot Talos CP VM")

	// VM-A gets a mgmt NIC so the test host can poll its COSI API for PeerStatus.
	vmACOSIPort := h.RandomPort()
	vmA := h.BootVM(t, "vm-a", vmlinuz, initramfs, kernelBase+" role=a listen_tcp=:50100",
		append(h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"),
			h.MgmtNIC(vmACOSIPort, 50100, "52:54:00:a0:00:02")...)...)
	sw.Lap("boot VM-A")

	allVMs := []*h.VM{vmA, vmB, vmDisc, vmCP}
	h.CleanupVMs(t, allVMs, out)

	// Discovery health check via HTTP from the test host.
	h.WaitForDiscoveryHTTP(t, discHTTPPort, 120*time.Second)
	sw.Lap("discovery HTTP ready")

	// Poll VM-A's PeerStatus via its TCP COSI API — verify WireGuard
	// handshake completes before waiting for probes to finish.
	vmAPeers, err := h.PollKubespandPeerStatus(t, fmt.Sprintf("127.0.0.1:%d", vmACOSIPort), 1, 180*time.Second)
	if err != nil {
		t.Errorf("VM-A peer status poll: %v", err)
	} else {
		t.Logf("VM-A peers up: %v", vmAPeers)
	}
	sw.Lap("VM-A peers up (host-side PeerStatus)")

	// Wait for VM-A to signal probes completed.
	h.RequireEvent(t, vmA, h.EventDone, 300*time.Second)
	sw.Lap("VM-A done (probes)")

	// Observe KubeSpan peer status from the Talos CP's API.
	// CP participates in the same mesh and sees kubespand VMs as peers.
	talosClient := h.NewTalosClient(t, h.RunfilePath(t, h.TalosConfig), fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
	defer talosClient.Close()
	h.WaitForTalosAPI(t, talosClient, cpIP, 180*time.Second)
	sw.Lap("Talos CP API ready")

	// Dump initial KubeSpan state — shows what the Talos CP discovered
	// before we start polling for peer "up" state.
	h.DumpKubeSpanDiagnostics(t, talosClient, cpIP)
	sw.Lap("initial diagnostics")

	peers, err := h.PollKubeSpanStatus(t, talosClient, cpIP, 120*time.Second)
	if err != nil {
		// Dump final state on failure to capture endpoint cycling, handshake attempts.
		h.DumpKubeSpanDiagnostics(t, talosClient, cpIP)
		t.Errorf("peer discovery via Talos API: %v", err)
	} else {
		t.Logf("KubeSpan peers observed from Talos CP: %v", peers)
	}
	sw.Lap("peer discovery via Talos API")

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
	sw.Lap("assertions")

	sw.Summary(out)
}
