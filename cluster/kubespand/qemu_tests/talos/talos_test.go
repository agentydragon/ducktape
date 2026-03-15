package talos_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/klauspost/compress/zstd"
	"github.com/siderolabs/talos/pkg/machinery/client"
	clientconfig "github.com/siderolabs/talos/pkg/machinery/client/config"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestTalosKubeSpanDoubleNAT(t *testing.T) {
	sw := h.NewStopwatch(t)

	talosImageZst := h.RunfilePath(t, h.TalosNocloudImagePath)
	alpineVmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	alpineInitramfsDisc := h.RunfilePath(t, h.DiscoveryInitramfs)
	alpineInitramfsRouter := h.RunfilePath(t, h.RouterInitramfs)
	sw.Lap("resolve runfiles")

	out := h.OutputDir(t)
	tmpDir := t.TempDir()

	talosBaseImage := filepath.Join(tmpDir, "nocloud-amd64.raw")
	decompressZstd(t, talosImageZst, talosBaseImage)
	sw.Lap("decompress talos image")

	vpsConfig := readRunfile(t, h.TalosVPSConfig)
	nat1Config := readRunfile(t, h.TalosNAT1Config)
	nat2Config := readRunfile(t, h.TalosNAT2Config)

	vpsCI := createCIDATA(t, tmpDir, "vps", vpsConfig)
	nat1CI := createCIDATA(t, tmpDir, "nat1", nat1Config)
	nat2CI := createCIDATA(t, tmpDir, "nat2", nat2Config)
	sw.Lap("create CIDATA volumes")

	mcastPortInternet := h.RandomPort()
	mcastPortLan1 := h.RandomPort()
	mcastPortLan2 := h.RandomPort()
	mcastInternet := fmt.Sprintf("230.0.0.1:%d", mcastPortInternet)
	mcastLan1 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan1)
	mcastLan2 := fmt.Sprintf("230.0.0.1:%d", mcastPortLan2)

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
	sw.Lap("boot infrastructure VMs (discovery + routers)")

	talosAPIPort := h.RandomPort()
	vmVPS := bootTalosVM(t, "talos-vps", talosBaseImage, vpsCI,
		talosAPIPort, h.McastNIC("net0", mcastInternet, "52:54:00:a0:00:01"))
	vmNAT1 := bootTalosVM(t, "talos-nat1", talosBaseImage, nat1CI,
		0, h.McastNIC("net0", mcastLan1, "52:54:00:a0:00:02"))
	vmNAT2 := bootTalosVM(t, "talos-nat2", talosBaseImage, nat2CI,
		0, h.McastNIC("net0", mcastLan2, "52:54:00:a0:00:03"))
	sw.Lap("boot Talos VMs")

	h.RequireAllEvents(t, []*h.VM{vmDiscovery, vmRouter1, vmRouter2}, h.EventDone, 30*time.Second)
	sw.Lap("infrastructure VMs ready")

	allVMs := []*h.VM{vmVPS, vmNAT1, vmNAT2, vmRouter1, vmRouter2, vmDiscovery}
	h.CleanupVMs(t, allVMs, out)

	// Create Talos API client from talosconfig credentials.
	endpoint := fmt.Sprintf("127.0.0.1:%d", talosAPIPort)
	nodeIP := "192.168.50.2"
	talosClient := newTalosClient(t, h.RunfilePath(t, h.TalosConfig), endpoint)
	defer talosClient.Close()

	// Observed on RBE (Firecracker, TCG): apid healthy ~64s after VM start.
	waitForTalosAPI(t, talosClient, nodeIP, 120*time.Second)
	sw.Lap("Talos API ready")

	// Observed: KubeSpan nftables rules applied ~35s after VM start.
	// Peer discovery depends on discovery service + WireGuard handshake.
	peers, err := pollKubeSpanStatus(t, talosClient, nodeIP, 120*time.Second)
	sw.Lap("KubeSpan status poll")

	statusJSON, _ := json.MarshalIndent(peers, "", "  ")
	h.SaveArtifact(t, out, "kubespan-status.json", string(statusJSON))

	if err != nil {
		t.Errorf("KubeSpan peer discovery failed: %v", err)
	}
	for _, peer := range peers {
		if peer.State != kubespan.PeerStateUp {
			t.Errorf("peer %s state=%s (want up), endpoint=%s", peer.Label, peer.State, peer.Endpoint)
		}
	}
	if len(peers) < 2 {
		t.Errorf("expected 2 KubeSpan peers, got %d", len(peers))
	}
	sw.Lap("assertions")

	sw.Summary(out)
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

// bootTalosVM starts a Talos QEMU VM from a raw disk image with CIDATA config.
// Uses snapshot=on so QEMU creates a temporary overlay per VM, avoiding full
// copies of the ~4.5 GB base image.
func bootTalosVM(t *testing.T, name, baseImage, cidataPath string, mgmtPort int, netArgs []string) *h.VM {
	t.Helper()

	args := []string{
		"-drive", fmt.Sprintf("file=%s,if=virtio,format=raw,snapshot=on", baseImage),
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
		args = append(args,
			"-netdev", fmt.Sprintf("user,id=mgmt,hostfwd=tcp::%d-:50000", mgmtPort),
			"-device", "virtio-net-pci,netdev=mgmt,mac=52:54:00:ab:00:01",
		)
	}

	return h.StartVM(t, name, exec.Command("qemu-system-x86_64", args...), false)
}

// newTalosClient creates a Talos API client from a talosconfig file.
func newTalosClient(t *testing.T, configPath, endpoint string) *client.Client {
	t.Helper()

	cfg, err := clientconfig.Open(configPath)
	if err != nil {
		t.Fatalf("open talosconfig: %v", err)
	}

	c, err := client.New(context.Background(),
		client.WithConfig(cfg),
		client.WithEndpoints(endpoint),
	)
	if err != nil {
		t.Fatalf("create talos client: %v", err)
	}

	return c
}

type kubespanPeerResult struct {
	Label    string             `json:"label"`
	State    kubespan.PeerState `json:"state"`
	Endpoint string             `json:"endpoint"`
}

// waitForTalosAPI polls client.Version() until the Talos API responds.
func waitForTalosAPI(t *testing.T, c *client.Client, nodeIP string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 5*time.Second)
		resp, err := c.Version(ctx)
		cancel()
		if err == nil {
			tag := ""
			for _, msg := range resp.Messages {
				if msg.Version != nil {
					tag = msg.Version.Tag
				}
			}
			t.Logf("talos API ready: %s", tag)
			return
		}
		t.Logf("waiting for talos API: %v", err)
		time.Sleep(1 * time.Second)
	}
	t.Fatalf("talos API not reachable after %v", timeout)
}

func pollKubeSpanStatus(t *testing.T, c *client.Client, nodeIP string, timeout time.Duration) ([]kubespanPeerResult, error) {
	t.Helper()

	deadline := time.Now().Add(timeout)
	var lastErr string

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 10*time.Second)
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, c.COSI)
		cancel()
		if err != nil {
			lastErr = err.Error()
			t.Logf("COSI poll (waiting): %s", lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		var peers []kubespanPeerResult
		for it := list.Iterator(); it.Next(); {
			ps := it.Value()
			peers = append(peers, kubespanPeerResult{
				Label:    ps.TypedSpec().Label,
				State:    ps.TypedSpec().State,
				Endpoint: ps.TypedSpec().Endpoint.String(),
			})
		}
		t.Logf("COSI poll: %d peers found", len(peers))

		allUp := len(peers) >= 2
		for _, p := range peers {
			if p.State != kubespan.PeerStateUp {
				allUp = false
			}
		}

		if allUp {
			return peers, nil
		}

		time.Sleep(1 * time.Second)
	}

	return nil, fmt.Errorf("timeout after %v, last error: %s", timeout, lastErr)
}

func decompressZstd(t *testing.T, src, dst string) {
	t.Helper()
	in, err := os.Open(src)
	if err != nil {
		t.Fatalf("open zstd source: %v", err)
	}
	defer in.Close()

	dec, err := zstd.NewReader(in)
	if err != nil {
		t.Fatalf("create zstd decoder: %v", err)
	}
	defer dec.Close()

	out, err := os.Create(dst)
	if err != nil {
		t.Fatalf("create output file: %v", err)
	}
	defer out.Close()

	if _, err := io.Copy(out, dec); err != nil {
		t.Fatalf("decompress zstd: %v", err)
	}
}
