package talos_test

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestTalosKubeSpanDoubleNAT(t *testing.T) {
	talosQcow2XZ := h.RunfilePath(t, h.TalosNocloudQcow2Path)
	alpineVmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	alpineInitramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	alpineInitramfsRouter := h.RunfilePath(t, h.RouterInitramfs)

	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	// Decompress Talos nocloud qcow2 base image.
	talosBaseQcow2 := filepath.Join(tmpDir, "nocloud-amd64.qcow2")
	xzCmd := exec.Command("xz", "-dk", "--stdout", talosQcow2XZ)
	baseFile, err := os.Create(talosBaseQcow2)
	if err != nil {
		t.Fatalf("create base qcow2: %v", err)
	}
	xzCmd.Stdout = baseFile
	xzCmd.Stderr = os.Stderr
	if err := xzCmd.Run(); err != nil {
		baseFile.Close()
		t.Fatalf("decompress talos qcow2: %v", err)
	}
	baseFile.Close()
	t.Logf("decompressed talos base image: %s", talosBaseQcow2)

	// Load pre-generated Talos machine configs (committed as testdata).
	// VPS is controlplane (trustd issues API certs for workers).
	vpsConfig := readRunfile(t, h.TalosVPSConfig)
	nat1Config := readRunfile(t, h.TalosNAT1Config)
	nat2Config := readRunfile(t, h.TalosNAT2Config)

	// Create CIDATA volumes.
	vpsCI := createCIDATA(t, tmpDir, "vps", vpsConfig)
	nat1CI := createCIDATA(t, tmpDir, "nat1", nat1Config)
	nat2CI := createCIDATA(t, tmpDir, "nat2", nat2Config)

	// 3 multicast bridges.
	mcastPortInternet := h.RandomPort()
	mcastPortLan1 := h.RandomPort()
	mcastPortLan2 := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLan1 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan1)
	mcastLan2 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan2)

	// Boot all VMs concurrently. Infrastructure VMs (discovery, routers)
	// signal readiness via events; Talos VMs boot in parallel.
	vmDiscovery := h.BootVM(t, "talos-disc", alpineVmlinuz, alpineInitramfsDisc,
		"mode=discovery role=discovery discovery_ip=192.168.50.254/24",
		h.McastNIC("net0", mcastInternet, "52:54:00:ff:00:01")...)
	vmRouter1 := h.BootVM(t, "talos-router-1", alpineVmlinuz, alpineInitramfsRouter,
		"mode=router role=router-1 internet_ip=192.168.50.1/24 lan_ip=192.168.60.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c1:00:01"),
			h.McastNIC("net1", mcastLan1, "52:54:00:c1:00:02")...)...)
	vmRouter2 := h.BootVM(t, "talos-router-2", alpineVmlinuz, alpineInitramfsRouter,
		"mode=router role=router-2 internet_ip=192.168.50.3/24 lan_ip=192.168.70.1/24",
		append(h.McastNIC("net0", mcastInternet, "52:54:00:c2:00:01"),
			h.McastNIC("net1", mcastLan2, "52:54:00:c2:00:02")...)...)

	talosAPIPort := h.RandomPort()
	vmVPS := bootTalosVM(t, "talos-vps", talosBaseQcow2, vpsCI,
		talosAPIPort, h.McastNIC("net0", mcastInternet, "52:54:00:a0:00:01"))
	vmNAT1 := bootTalosVM(t, "talos-nat1", talosBaseQcow2, nat1CI,
		0, h.McastNIC("net0", mcastLan1, "52:54:00:a0:00:02"))
	vmNAT2 := bootTalosVM(t, "talos-nat2", talosBaseQcow2, nat2CI,
		0, h.McastNIC("net0", mcastLan2, "52:54:00:a0:00:03"))

	// Wait for infrastructure to be ready (fail fast if any crashes).
	h.RequireEvent(t, vmDiscovery, h.EventDone, 30*time.Second)
	h.RequireEvent(t, vmRouter1, h.EventDone, 30*time.Second)
	h.RequireEvent(t, vmRouter2, h.EventDone, 30*time.Second)

	// Ensure VM logs are always saved, even on Fatalf.
	allVMs := []*h.VM{vmVPS, vmNAT1, vmNAT2, vmRouter1, vmRouter2, vmDiscovery}
	t.Cleanup(func() {
		h.KillAndWait(allVMs...)
		for _, vm := range allVMs {
			vm.SaveLogs(t, out)
		}
	})

	// 7. Wait for Talos API, then poll KubeSpan status.
	tc := &talosctl{
		bin:        h.RunfilePath(t, h.TalosctlPath),
		configPath: h.RunfilePath(t, h.TalosConfig),
		endpoint:   fmt.Sprintf("127.0.0.1:%d", talosAPIPort),
		nodeIP:     "192.168.50.2",
	}
	// Observed on RBE (Firecracker, TCG): apid healthy ~64s after VM start.
	waitForTalosAPI(t, tc, 120*time.Second)
	// Observed: KubeSpan nftables rules applied ~35s after VM start.
	// Peer discovery depends on discovery service + WireGuard handshake.
	result := pollKubeSpanStatus(t, tc, 120*time.Second)

	statusJSON, _ := json.MarshalIndent(result, "", "  ")
	h.SaveArtifact(t, out, "kubespan-status.json", string(statusJSON))

	if !result.success {
		t.Errorf("KubeSpan peer discovery failed: %s", result.failReason)
	}
	for _, peer := range result.peers {
		if peer.State != "up" {
			t.Errorf("peer %s state=%s (want up), endpoint=%s", peer.Label, peer.State, peer.Endpoint)
		}
	}
	if len(result.peers) < 2 {
		t.Errorf("expected 2 KubeSpan peers, got %d", len(result.peers))
	}
}

func readRunfile(t *testing.T, path string) []byte {
	t.Helper()
	p := h.RunfilePath(t, path)
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return data
}

func createCIDATA(t *testing.T, tmpDir, name string, machineConfig []byte) string {
	t.Helper()

	ciDir := filepath.Join(tmpDir, "cidata-"+name)
	os.MkdirAll(ciDir, 0o755)

	metaData := fmt.Sprintf("instance-id: %s\nlocal-hostname: %s\n", name, name)
	os.WriteFile(filepath.Join(ciDir, "meta-data"), []byte(metaData), 0o644)
	os.WriteFile(filepath.Join(ciDir, "user-data"), machineConfig, 0o644)

	imgPath := filepath.Join(tmpDir, fmt.Sprintf("cidata-%s.img", name))

	h.RunCmd(t, "dd", "if=/dev/zero", "of="+imgPath, "bs=1M", "count=4")
	h.RunCmd(t, "/usr/sbin/mkfs.vfat", "-n", "cidata", imgPath)
	h.RunCmd(t, "/usr/bin/mcopy", "-i", imgPath, filepath.Join(ciDir, "meta-data"), "::")
	h.RunCmd(t, "/usr/bin/mcopy", "-i", imgPath, filepath.Join(ciDir, "user-data"), "::")

	t.Logf("created CIDATA for %s: %s", name, imgPath)
	return imgPath
}

// bootTalosVM starts a Talos QEMU VM from a qcow2 disk image with CIDATA config.
// Creates a COW overlay so each VM has its own writable copy.
func bootTalosVM(t *testing.T, name, baseQcow2, cidataPath string, mgmtPort int, netArgs []string) *h.VM {
	t.Helper()

	// Copy base image — each VM needs its own writable copy.
	disk := filepath.Join(filepath.Dir(cidataPath), name+".qcow2")
	src, err := os.ReadFile(baseQcow2)
	if err != nil {
		t.Fatalf("read base qcow2: %v", err)
	}
	if err := os.WriteFile(disk, src, 0o644); err != nil {
		t.Fatalf("write vm disk: %v", err)
	}

	args := []string{
		"-drive", fmt.Sprintf("file=%s,if=virtio,format=qcow2", disk),
		"-drive", fmt.Sprintf("file=%s,if=virtio,format=raw,readonly=on", cidataPath),
		"-nographic",
		"-m", "1536",
		"-machine", "accel=tcg",
		"-cpu", "max",
		"-display", "none",
		"-smp", "2",
	}

	args = append(args, netArgs...)

	if mgmtPort > 0 {
		// Extra user-mode NIC for talosctl access from the host.
		// Forwards host localhost:mgmtPort → VM port 50000 (Talos apid).
		// The mcast NICs carry inter-VM traffic only (no host access).
		// machine.certSANs must include 127.0.0.1 for the TLS handshake to succeed.
		args = append(args,
			"-netdev", fmt.Sprintf("user,id=mgmt,hostfwd=tcp::%d-:50000", mgmtPort),
			"-device", "virtio-net-pci,netdev=mgmt,mac=52:54:00:ab:00:01",
		)
	}

	return h.StartVM(t, name, exec.Command("qemu-system-x86_64", args...), false)
}

// talosctl wraps talosctl invocations with pre-configured connection params.
type talosctl struct {
	bin        string
	configPath string
	endpoint   string // host:port for transport (port-forwarded)
	nodeIP     string // VM's actual IP (no port, used as node identity)
}

func (tc *talosctl) run(args ...string) *exec.Cmd {
	// Per-subcommand flags must come AFTER the subcommand name.
	// --endpoints: transport address (host:port for our port forward)
	// --nodes: target node identity (just IP, no port — apid uses this
	//   to route requests in multi-node setups)
	// machine.certSANs includes 127.0.0.1 so the apid cert is valid
	// for our localhost port-forwarded connection.
	full := append(args,
		"--talosconfig", tc.configPath,
		"--nodes", tc.nodeIP,
		"--endpoints", tc.endpoint,
	)
	return exec.Command(tc.bin, full...)
}

type kubespanPeerResult struct {
	Label    string `json:"label"`
	State    string `json:"state"`
	Endpoint string `json:"endpoint"`
}

type talosResource struct {
	Metadata struct {
		ID string `json:"id"`
	} `json:"metadata"`
	Spec struct {
		State    string `json:"state"`
		Endpoint string `json:"endpoint"`
		Label    string `json:"label"`
	} `json:"spec"`
}

type kubespanResult struct {
	success    bool
	failReason string
	peers      []kubespanPeerResult
	rawOutput  string
}

func (r kubespanResult) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Success    bool                 `json:"success"`
		FailReason string               `json:"fail_reason,omitempty"`
		Peers      []kubespanPeerResult `json:"peers"`
		RawOutput  string               `json:"raw_output"`
	}{r.success, r.failReason, r.peers, r.rawOutput})
}

// waitForTalosAPI polls talosctl version until the Talos API responds.
func waitForTalosAPI(t *testing.T, tc *talosctl, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		out, err := tc.run("version", "--short").CombinedOutput()
		if err == nil {
			t.Logf("talos API ready: %s", strings.TrimSpace(string(out)))
			return
		}
		t.Logf("waiting for talos API: %s", strings.TrimSpace(string(out)))
		time.Sleep(10 * time.Second)
	}
	t.Fatalf("talos API not reachable after %v", timeout)
}

func pollKubeSpanStatus(t *testing.T, tc *talosctl, timeout time.Duration) kubespanResult {
	t.Helper()

	deadline := time.Now().Add(timeout)
	var lastOutput string
	var lastErr string

	for time.Now().Before(deadline) {
		out, err := tc.run("get", "kubespanpeerstatuses", "-o", "json").CombinedOutput()
		lastOutput = string(out)
		if err != nil {
			lastErr = err.Error()
			t.Logf("talosctl poll (waiting): %s: %s", lastErr, strings.TrimSpace(lastOutput))
			time.Sleep(10 * time.Second)
			continue
		}

		peers := parsePeerStatuses(lastOutput)
		t.Logf("talosctl poll: %d peers found", len(peers))

		allUp := len(peers) >= 2
		for _, p := range peers {
			if p.State != "up" {
				allUp = false
			}
		}

		if allUp {
			return kubespanResult{
				success:   true,
				peers:     peers,
				rawOutput: lastOutput,
			}
		}

		time.Sleep(10 * time.Second)
	}

	return kubespanResult{
		success:    false,
		failReason: fmt.Sprintf("timeout after %v, last error: %s", timeout, lastErr),
		rawOutput:  lastOutput,
	}
}

func parsePeerStatuses(output string) []kubespanPeerResult {
	var peers []kubespanPeerResult

	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}

		var res talosResource
		if err := json.Unmarshal([]byte(line), &res); err != nil {
			continue
		}

		peers = append(peers, kubespanPeerResult{
			Label:    res.Metadata.ID,
			State:    res.Spec.State,
			Endpoint: res.Spec.Endpoint,
		})
	}
	return peers
}
