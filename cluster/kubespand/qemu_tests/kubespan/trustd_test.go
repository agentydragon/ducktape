package kubespan_test

import (
	"fmt"
	"path/filepath"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

// TestTrustdCSRFlow verifies that the trustd CSR flow works for both kubespand
// and Talos workers. The CP's trustd service signs CSRs, enabling workers to
// serve mTLS on their API port.
//
// Topology: discovery VM + Talos CP VM + worker VM on 192.168.50.0/24.
//
// For kubespand: apid on port 50000 is accessible via Talos API client.
// For Talos: the native Talos API is accessible (CSR flow is built-in).
func TestTrustdCSRFlow(t *testing.T) {
	t.Parallel()
	for _, wt := range []h.NodeType{h.NodeTypeKubespand, h.NodeTypeTalos} {
		t.Run(string(wt), func(t *testing.T) {
			t.Parallel()
			runTrustdCSRFlow(t, wt)
		})
	}
}

func runTrustdCSRFlow(t *testing.T, workerType h.NodeType) {
	sw := h.NewStopwatch(t)

	// Resolve runfiles.
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	sw.Lap("resolve runfiles")

	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	// Generate all crypto material and configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	cpIP := "192.168.50.2"
	discIP := "192.168.50.254"
	workerIP := "192.168.50.1"

	cpEndpoint := h.ControlPlaneEndpoint(cpIP)
	discEndpoint := h.DiscoveryEndpoint(discIP)

	vpsConfigData := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   cpIP + "/24",
		ControlPlaneEndpoint: cpEndpoint,
		DiscoveryEndpoint:    discEndpoint,
		CertSANs:             []string{cpIP, "127.0.0.1"},
	})
	talosConfigPath := filepath.Join(tmpDir, "talosconfig")
	secrets.WriteTalosconfig(t, talosConfigPath)
	sw.Lap("generate talos configs")

	// Create CIDATA for the Talos CP VM.
	vpsCI := h.CreateCIDATA(t, tmpDir, "vps", vpsConfigData)
	sw.Lap("create CIDATA")

	// All VMs on the same flat L2 segment.
	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	vmDisc := h.BootVM(t, "trustd-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("mode=discovery role=discovery discovery_ip=%s/24", discIP),
		h.McastNIC("net0", mcastAddr, h.DiscoveryMAC))
	sw.Lap("boot discovery VM")

	// Talos CP VM — provides trustd on port 50001.
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "trustd-cp", talosBaseImage, vpsCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, h.NodeAMAC))
	sw.Lap("boot Talos CP VM")

	// Boot worker based on workerType.
	var workerVM *h.VM
	var workerAPIPort int

	switch workerType {
	case h.NodeTypeKubespand:
		initramfsTrustd := h.RunfilePath(t, h.TrustdInitramfs)
		kubespandCfg := h.NewTestAgentConfig(creds, discIP+":3000", cpEndpoint)
		kubespandCI := h.CreateKubespandCIDATA(t, tmpDir, "trustd-worker", kubespandCfg)

		workerAPIPort = h.RandomPort()
		workerVM = h.BootVM(t, "trustd-worker", vmlinuz, initramfsTrustd, "",
			append(h.McastNIC("net0", mcastAddr, h.NodeBMAC), h.CIDATADrive(kubespandCI)...),
			h.PortForward{HostPort: workerAPIPort, GuestPort: h.ApidGuestPort})

	case h.NodeTypeTalos:
		workerConfig := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   workerIP + "/24",
			ControlPlaneEndpoint: cpEndpoint,
			DiscoveryEndpoint:    discEndpoint,
		})
		workerCI := h.CreateCIDATA(t, tmpDir, "trustd-worker", workerConfig)

		workerAPIPort = h.RandomPort()
		workerVM = h.BootTalosVM(t, "trustd-worker", talosBaseImage, workerCI,
			workerAPIPort, h.McastNIC("net0", mcastAddr, h.NodeBMAC))
	}
	sw.Lap("boot worker VM")

	allVMs := []*h.VM{vmCP, workerVM, vmDisc}
	h.CleanupVMs(t, allVMs, out)

	// Wait for discovery service.
	vmDisc.WaitForProbeServer(120 * time.Second)
	sw.Lap("discovery VM ready")

	cpNode := h.NewTalosMeshNode(t, vmCP, cpIP, talosConfigPath, talosAPIPort)
	workerNode := h.NewTalosMeshNode(t, workerVM, workerIP, talosConfigPath, workerAPIPort)

	// Run convergence loop: worker becoming "ready" (COSI watches established)
	// proves the trustd CSR flow succeeded — the worker's TalosClient connecting
	// requires a signed certificate from the CP's trustd service.
	runner := &h.MeshTestRunner{
		T:           t,
		Nodes:       []*h.MeshNode{cpNode, workerNode},
		Stopwatch:   sw,
		OutDir:      out,
		SuccessFunc: h.WorkerAPISuccess(workerVM.Name),
	}
	runner.Run(t.Context())
}
