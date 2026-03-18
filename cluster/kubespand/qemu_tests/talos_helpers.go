// Shared helpers for booting Talos VMs and interacting with the Talos API
// in QEMU integration tests.
package qemu_tests

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/controller/generic"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/client"
	clientconfig "github.com/siderolabs/talos/pkg/machinery/client/config"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
)

// pollUntil calls fn every second until it returns true or the deadline passes.
// Returns true if fn returned true, false on timeout.
func pollUntil(deadline time.Time, fn func() bool) bool {
	for time.Now().Before(deadline) {
		if fn() {
			return true
		}
		time.Sleep(1 * time.Second)
	}
	return false
}

// BootTalosVM starts a Talos QEMU VM from a qcow2 disk image with CIDATA config.
// Uses snapshot=on so QEMU creates a temporary overlay per VM, keeping the
// base image read-only and allowing multiple VMs to share it.
func BootTalosVM(t *testing.T, name, baseImage, cidataPath string, mgmtPort int, netArgs []string) *VM {
	t.Helper()

	args := []string{
		"-drive", fmt.Sprintf("file=%s,if=virtio,format=qcow2,snapshot=on", baseImage),
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
		args = append(args, MgmtNIC(mgmtPort, 50000, "52:54:00:ab:00:01")...)
	}

	return StartVM(t, name, exec.Command("qemu-system-x86_64", args...))
}

// CreateCIDATA creates a FAT32 disk image with cloud-init metadata for Talos.
func CreateCIDATA(t *testing.T, tmpDir, name string, machineConfig []byte) string {
	t.Helper()

	ciDir := filepath.Join(tmpDir, "cidata-"+name)
	os.MkdirAll(ciDir, 0o755)

	metaData := fmt.Sprintf("instance-id: %s\nlocal-hostname: %s\n", name, name)
	os.WriteFile(filepath.Join(ciDir, "meta-data"), []byte(metaData), 0o644)
	os.WriteFile(filepath.Join(ciDir, "user-data"), machineConfig, 0o644)

	imgPath := filepath.Join(tmpDir, fmt.Sprintf("cidata-%s.img", name))

	RunCmd(t, "dd", "if=/dev/zero", "of="+imgPath, "bs=1M", "count=4")
	RunCmd(t, "/usr/sbin/mkfs.vfat", "-n", "cidata", imgPath)
	RunCmd(t, "/usr/bin/mcopy", "-i", imgPath, filepath.Join(ciDir, "meta-data"), "::")
	RunCmd(t, "/usr/bin/mcopy", "-i", imgPath, filepath.Join(ciDir, "user-data"), "::")

	t.Logf("created CIDATA for %s: %s", name, imgPath)
	return imgPath
}

// NewTalosClient creates a Talos API client from a talosconfig file.
func NewTalosClient(t *testing.T, configPath, endpoint string) *client.Client {
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

// dumpCOSIList lists all resources of type T and logs each one with %+v.
func dumpCOSIList[T generic.ResourceWithRD](t *testing.T, ctx context.Context, st state.State, label string) {
	t.Helper()
	list, err := safe.StateListAll[T](ctx, st)
	if err != nil {
		t.Logf("  %s: error: %v", label, err)
		return
	}
	items := collectList(list)
	t.Logf("  %s: %d total %+v", label, len(items), items)
}

// DumpKubeSpanDiagnostics queries COSI resources from a Talos node via the Talos
// API and logs them for debugging WireGuard handshake issues.
func DumpKubeSpanDiagnostics(t *testing.T, c *client.Client, nodeIP string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 15*time.Second)
	defer cancel()

	t.Log("=== KubeSpan Diagnostics ===")
	dumpCOSIList[*kubespan.Identity](t, ctx, c.COSI, "Identity")
	dumpCOSIList[*kubespan.Config](t, ctx, c.COSI, "Config")
	dumpCOSIList[*cluster.Affiliate](t, ctx, c.COSI, "Affiliates")
	dumpCOSIList[*kubespan.PeerSpec](t, ctx, c.COSI, "PeerSpecs")
	dumpCOSIList[*kubespan.PeerStatus](t, ctx, c.COSI, "PeerStatuses")
	t.Log("=== End KubeSpan Diagnostics ===")
}
