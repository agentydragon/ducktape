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

func runTopology(t *testing.T, topology string, workerType h.NodeType) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	kubespandInitramfs := h.RunfilePath(t, h.KubespanInitramfs)
	talosBaseImage := h.RunfilePath(t, h.TalosNocloudImagePath)
	out := h.OutputDir(t)
	sw.Lap("resolve runfiles")

	mcastPort := h.RandomPort()
	mcastAddr := fmt.Sprintf("230.0.0.1:%d", mcastPort)

	// Topology-specific parameters.
	var cpIP, discIP string
	var linkIPA, linkIPB string
	var cpRoutes, workerARoutes, workerBRoutes []*v1alpha1.Route

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
		cpRoutes = []*v1alpha1.Route{
			{RouteNetwork: "10.1.0.0/24"},
			{RouteNetwork: "10.2.0.0/24"},
		}
		workerARoutes = []*v1alpha1.Route{
			{RouteNetwork: "10.2.0.0/24"},
			{RouteNetwork: "10.0.0.0/24"},
		}
		workerBRoutes = []*v1alpha1.Route{
			{RouteNetwork: "10.1.0.0/24"},
			{RouteNetwork: "10.0.0.0/24"},
		}
	}

	discAddr := discIP + ":3000"
	cpEndpoint := h.ControlPlaneEndpoint(cpIP)
	discEndpoint := h.DiscoveryEndpoint(discIP)
	tmpDir := t.TempDir()

	// Generate all crypto material and Talos configs at test time.
	secrets := h.GenerateTestTalosSecrets(t)
	creds := secrets.Creds()

	cpConfigData := secrets.ControlPlaneConfig(h.TalosNodeConfig{
		IP:                   fmt.Sprintf("%s/24", cpIP),
		Routes:               cpRoutes,
		ControlPlaneEndpoint: cpEndpoint,
		DiscoveryEndpoint:    discEndpoint,
		CertSANs:             []string{cpIP, "127.0.0.1"},
	})

	talosConfigPath := filepath.Join(tmpDir, "talosconfig")
	secrets.WriteTalosconfig(t, talosConfigPath)
	sw.Lap("generate talos configs")

	// Discovery VM with extra forward for HTTP health check from the test host.
	vmDisc := h.BootVM(t, "vm-disc", vmlinuz, initramfsDisc,
		fmt.Sprintf("role=discovery discovery_ip=%s/24 topology=%s", discIP, topology),
		h.McastNIC("net0", mcastAddr, h.DiscoveryMAC),
		h.PortForward{GuestPort: 3000})
	sw.Lap("boot discovery VM")

	// Boot workers based on workerType.
	var workerA, workerB *h.MeshNode
	var allVMs []*h.VM

	switch workerType {
	case h.NodeTypeKubespand:
		cfgB := h.NewTestAgentConfig(creds, discAddr, cpEndpoint)
		cfgB.Network.Interface = "eth0"
		cfgB.Network.Routes = workerBRoutes
		cidataB := h.CreateKubespandCIDATA(t, tmpDir, "vm-b", cfgB)

		bAPIPort := h.RandomPort()
		vmB := h.BootVM(t, "vm-b", vmlinuz, kubespandInitramfs,
			fmt.Sprintf("role=vm-b link_ip=%s", linkIPB),
			append(h.McastNIC("net0", mcastAddr, h.NodeBMAC), h.CIDATADrive(cidataB)...),
			h.PortForward{GuestPort: h.COSIGuestPort},
			h.PortForward{HostPort: bAPIPort, GuestPort: h.ApidGuestPort})
		sw.Lap("boot VM-B")

		cfgA := h.NewTestAgentConfig(creds, discAddr, cpEndpoint)
		cfgA.Network.Interface = "eth0"
		cfgA.Network.Routes = workerARoutes
		cidataA := h.CreateKubespandCIDATA(t, tmpDir, "vm-a", cfgA)

		aAPIPort := h.RandomPort()
		vmA := h.BootVM(t, "vm-a", vmlinuz, kubespandInitramfs,
			fmt.Sprintf("role=vm-a link_ip=%s", linkIPA),
			append(h.McastNIC("net0", mcastAddr, h.NodeAMAC), h.CIDATADrive(cidataA)...),
			h.PortForward{GuestPort: h.COSIGuestPort},
			h.PortForward{HostPort: aAPIPort, GuestPort: h.ApidGuestPort})
		sw.Lap("boot VM-A")

		workerA = h.NewTalosMeshNode(t, vmA, linkIPA, talosConfigPath, aAPIPort)
		workerB = h.NewTalosMeshNode(t, vmB, linkIPB, talosConfigPath, bAPIPort)
		allVMs = []*h.VM{vmA, vmB, vmDisc}

	case h.NodeTypeTalos:
		workerAConfig := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   fmt.Sprintf("%s/24", linkIPA),
			Routes:               workerARoutes,
			ControlPlaneEndpoint: cpEndpoint,
			DiscoveryEndpoint:    discEndpoint,
		})
		workerBConfig := secrets.WorkerConfig(h.TalosNodeConfig{
			IP:                   fmt.Sprintf("%s/24", linkIPB),
			Routes:               workerBRoutes,
			ControlPlaneEndpoint: cpEndpoint,
			DiscoveryEndpoint:    discEndpoint,
		})
		aCI := h.CreateCIDATA(t, tmpDir, "vm-a", workerAConfig)
		bCI := h.CreateCIDATA(t, tmpDir, "vm-b", workerBConfig)

		aAPIPort := h.RandomPort()
		bAPIPort := h.RandomPort()
		vmA := h.BootTalosVM(t, "vm-a", talosBaseImage, aCI,
			aAPIPort, h.McastNIC("net0", mcastAddr, h.NodeAMAC))
		vmB := h.BootTalosVM(t, "vm-b", talosBaseImage, bCI,
			bAPIPort, h.McastNIC("net0", mcastAddr, h.NodeBMAC))
		sw.Lap("boot Talos workers")

		workerA = h.NewTalosMeshNode(t, vmA, linkIPA, talosConfigPath, aAPIPort)
		workerB = h.NewTalosMeshNode(t, vmB, linkIPB, talosConfigPath, bAPIPort)
		allVMs = []*h.VM{vmA, vmB, vmDisc}
	}
	// Talos CP VM — participates in KubeSpan mesh for API-based peer observation.
	cpCI := h.CreateCIDATA(t, tmpDir, "cp", cpConfigData)
	talosAPIPort := h.RandomPort()
	vmCP := h.BootTalosVM(t, "vm-cp", talosBaseImage, cpCI,
		talosAPIPort, h.McastNIC("net0", mcastAddr, h.NodeCPMAC))
	sw.Lap("boot Talos CP VM")

	allVMs = append(allVMs, vmCP)
	h.CleanupVMs(t, allVMs, out)

	// Discovery health check via HTTP from the test host.
	h.WaitForDiscoveryHTTP(t, vmDisc.ForwardAddr(3000), 120*time.Second)
	sw.Lap("discovery HTTP ready")

	// Talos CP API.
	cpNode := h.NewTalosMeshNode(t, vmCP, cpIP, talosConfigPath, talosAPIPort)

	// Run convergence loop: watches COSI on all nodes, streams dmesg,
	// fires probes when peers come up, exits on full mesh + probes pass.
	runner := &h.MeshTestRunner{
		T:            t,
		Nodes:        []*h.MeshNode{cpNode, workerA, workerB},
		Stopwatch:    sw,
		OutDir:       out,
		SuccessFunc:  h.FullMeshSuccess(2), // each node expects 2 peers
		ProbeTargets: h.ULAProbeTargets,
	}
	runner.Run(t.Context())

	summary := map[string]interface{}{
		"topology":    topology,
		"worker_type": string(workerType),
		"cluster_id":  creds.ClusterID,
		"mcast_port":  mcastPort,
	}
	summaryJSON, _ := json.MarshalIndent(summary, "", "  ")
	h.SaveArtifact(t, out, "test-summary.json", string(summaryJSON))
}
