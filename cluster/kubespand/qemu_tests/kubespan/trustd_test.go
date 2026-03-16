package kubespan_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

// TestTrustdCSRFlow verifies that kubespand can obtain TLS certificates from
// a Talos controlplane node's trustd service via the standard CSR flow.
// Topology: discovery VM + Talos CP VM + kubespand VM on 192.168.50.0/24.
// The kubespand VM runs apid on port 50000 — if we can connect via Talos API,
// that proves the trustd CSR flow produced secrets.API successfully.
func TestTrustdCSRFlow(t *testing.T) {
	t.Parallel()
	sw := h.NewStopwatch(t)

	// Resolve runfiles.
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	initramfsTrustd := h.RunfilePath(t, h.TrustdInitramfs)
	sw.Lap("resolve runfiles")

	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	// Generate all crypto material and configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	vpsConfigData := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   "192.168.50.2/24",
		ControlPlaneEndpoint: "https://192.168.50.2:6443",
		DiscoveryEndpoint:    "http://192.168.50.254:3000",
		EndpointFilters:      []string{"192.168.50.0/24"},
		CertSANs:             []string{"192.168.50.2", "127.0.0.1"},
	})
	talosConfigPath := secrets.WriteTalosconfig(t, tmpDir)
	sw.Lap("generate talos configs")

	// Create CIDATA for the Talos CP VM.
	vpsCI := h.CreateCIDATA(t, tmpDir, "vps", vpsConfigData)
	sw.Lap("create CIDATA")

	// All VMs on the same flat L2 segment.
	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	vmDisc := h.BootVM(t, "trustd-disc", vmlinuz, initramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01"))
	sw.Lap("boot discovery VM")

	// Talos CP VM — provides trustd on port 50001.
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "trustd-cp", talosBaseImage, vpsCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"))
	sw.Lap("boot Talos CP VM")

	// Build kubespand agent config with trustd CSR flow credentials.
	kubespandCfg := h.NewTestAgentConfig(creds, "192.168.50.254:3000")
	kubespandCfg.Cluster.Endpoint = "https://192.168.50.2:6443"
	kubespandCfg.Kubespan.ListenPort = 51820
	kubespandCfg.Kubespan.EndpointFilters = []string{"192.168.50.0/24"}
	kubespandCfg.Api.CACrt = creds.CACrt
	kubespandCfg.Api.Token = creds.MachineToken
	kubespandCfg.Api.ApidPath = "/apid"
	// Include 127.0.0.1 in cert SANs for port-forwarded test connections.
	kubespandCfg.Api.CertSANs = []string{"127.0.0.1"}
	kubespandCI := h.CreateKubespandCIDATA(t, tmpDir, "kubespand", kubespandCfg)

	// kubespand VM with CIDATA config. Extra forward for apid port 50000.
	vmKubespand := h.BootVM(t, "trustd-kubespand", vmlinuz, initramfsTrustd, "",
		append(h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"), h.CIDATADrive(kubespandCI)...),
		h.PortForward{GuestPort: 50000})
	sw.Lap("boot kubespand VM")

	allVMs := []*h.VM{vmCP, vmKubespand, vmDisc}
	h.CleanupVMs(t, allVMs, out)

	// On failure, dump all available diagnostics before cleanup kills the VMs.
	t.Cleanup(func() {
		if !t.Failed() {
			return
		}
		t.Log("=== FAILURE DIAGNOSTICS ===")

		// kubespand VM raw log (stderr from diagnostics dumps inside the VM).
		rawLog := vmKubespand.GetRawLog()
		h.SaveArtifact(t, out, "trustd-kubespand-raw.log", rawLog)
		lines := strings.Split(rawLog, "\n")
		start := len(lines) - 300
		if start < 0 {
			start = 0
		}
		t.Logf("--- kubespand VM raw log (last %d of %d lines) ---", len(lines)-start, len(lines))
		for _, line := range lines[start:] {
			if line != "" {
				t.Log(line)
			}
		}

		// Talos CP diagnostics (if API was available).
		t.Log("--- Talos CP KubeSpan diagnostics (post-failure) ---")
		client := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
		defer client.Close()
		h.DumpKubeSpanDiagnostics(t, client, "192.168.50.2")

		t.Log("=== END FAILURE DIAGNOSTICS ===")
	})

	// Wait for discovery service (probe server responding = VM ready).
	vmDisc.WaitForProbeServer(120 * time.Second)
	sw.Lap("discovery VM ready")

	// Wait for Talos CP API (boot takes ~60-120s on TCG).
	talosClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
	defer talosClient.Close()
	h.WaitForTalosAPI(t, talosClient, "192.168.50.2", 180*time.Second)
	sw.Lap("Talos CP API ready")

	// Dump Talos CP's KubeSpan state before waiting for kubespand apid.
	h.DumpKubeSpanDiagnostics(t, talosClient, "192.168.50.2")
	sw.Lap("initial CP diagnostics")

	// Connect to kubespand's apid — success proves the full chain:
	// kubespand → OSRootController → APICertSANsController → APIController
	// → trustd CSR → secrets.API → apid serves mTLS on :50000.
	kubespandClient := h.NewTalosClient(t, talosConfigPath, vmKubespand.ForwardAddr(50000))
	defer kubespandClient.Close()
	h.WaitForTalosAPI(t, kubespandClient, "192.168.50.1", 300*time.Second)
	sw.Lap("kubespand apid ready (trustd CSR flow succeeded)")

	sw.Summary(out)
}
