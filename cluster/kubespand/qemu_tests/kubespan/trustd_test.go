package kubespan_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

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

	// Parse Talos CP config to extract credentials for kubespand.
	vpsConfigData := h.ReadRunfile(t, h.TalosVPSConfig)
	var cfg trustdTalosConfig
	if err := yaml.Unmarshal(vpsConfigData, &cfg); err != nil {
		t.Fatalf("parse talos config: %v", err)
	}
	sw.Lap("parse talos config")

	// Create CIDATA for the Talos CP VM.
	vpsCI := h.CreateCIDATA(t, tmpDir, "vps", vpsConfigData)
	sw.Lap("create CIDATA")

	// All VMs on the same flat L2 segment.
	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	vmDisc := h.BootVM(t, "trustd-disc", vmlinuz, initramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastAddr, "52:54:00:ff:00:01")...)
	sw.Lap("boot discovery VM")

	// Talos CP VM — provides trustd on port 50001.
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "trustd-cp", talosBaseImage, vpsCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, "52:54:00:a0:00:01"))
	sw.Lap("boot Talos CP VM")

	// kubespand VM with CA cert + token for the trustd CSR flow.
	// mgmt NIC forwards apid port 50000 so the test can observe from outside.
	kubespandAPIPort := h.RandomPort()
	kernelArgs := fmt.Sprintf(
		"cluster_id=%s shared_secret=%s discovery=192.168.50.254:3000 ca_crt=%s token=%s cluster_endpoint=https://192.168.50.2:6443",
		cfg.Cluster.ID, cfg.Cluster.Secret, cfg.Machine.CA.Crt, cfg.Machine.Token,
	)
	mgmtNIC := []string{
		"-netdev", fmt.Sprintf("user,id=mgmt,hostfwd=tcp::%d-:50000", kubespandAPIPort),
		"-device", "virtio-net-pci,netdev=mgmt,mac=52:54:00:ab:00:01",
	}
	vmKubespand := h.BootVM(t, "trustd-kubespand", vmlinuz, initramfsTrustd, kernelArgs,
		append(h.McastNIC("net0", mcastAddr, "52:54:00:b0:00:01"), mgmtNIC...)...)
	sw.Lap("boot kubespand VM")

	allVMs := []*h.VM{vmCP, vmKubespand, vmDisc}
	h.CleanupVMs(t, allVMs, out)

	// On failure, dump all available diagnostics before cleanup kills the VMs.
	var talosConfigPath string
	t.Cleanup(func() {
		if !t.Failed() {
			return
		}
		t.Log("=== FAILURE DIAGNOSTICS ===")

		// kubespand VM events (CSR flow status, process health).
		t.Log("--- kubespand VM events ---")
		for _, evt := range vmKubespand.GetEvents() {
			t.Logf("  [%s] %s (error=%s)", evt.Type, evt.Message, evt.Error)
		}

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
		if talosConfigPath != "" {
			t.Log("--- Talos CP KubeSpan diagnostics (post-failure) ---")
			client := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", talosAPIPort))
			defer client.Close()
			h.DumpKubeSpanDiagnostics(t, client, "192.168.50.2")
		}

		t.Log("=== END FAILURE DIAGNOSTICS ===")
	})

	// Wait for discovery service.
	h.RequireEvent(t, vmDisc, h.EventDone, 120*time.Second)
	sw.Lap("discovery VM ready")

	// Wait for Talos CP API (boot takes ~60-120s on TCG).
	talosConfigPath = h.RunfilePath(t, h.TalosConfig)
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
	kubespandClient := h.NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", kubespandAPIPort))
	defer kubespandClient.Close()
	h.WaitForTalosAPI(t, kubespandClient, "192.168.50.1", 300*time.Second)
	sw.Lap("kubespand apid ready (trustd CSR flow succeeded)")

	sw.Summary(out)
}

// trustdTalosConfig holds the subset of Talos machine config needed by TestTrustdCSRFlow.
type trustdTalosConfig struct {
	Machine struct {
		Token string `yaml:"token"`
		CA    struct {
			Crt string `yaml:"crt"`
		} `yaml:"ca"`
	} `yaml:"machine"`
	Cluster struct {
		ID     string `yaml:"id"`
		Secret string `yaml:"secret"`
	} `yaml:"cluster"`
}
