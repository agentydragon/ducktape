// Shared helpers for booting Talos VMs and interacting with the Talos API
// in QEMU integration tests.
package qemu_tests

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/siderolabs/talos/pkg/machinery/client"
	clientconfig "github.com/siderolabs/talos/pkg/machinery/client/config"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
)

// KubespanPeerResult holds the result of a KubeSpan peer status query.
type KubespanPeerResult struct {
	Label    string             `json:"label"`
	State    kubespan.PeerState `json:"state"`
	Endpoint string             `json:"endpoint"`
}

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

	return StartVM(t, name, exec.Command("qemu-system-x86_64", args...), false)
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

// WaitForTalosAPI polls client.Version() until the Talos API responds.
func WaitForTalosAPI(t *testing.T, c *client.Client, nodeIP string, timeout time.Duration) {
	t.Helper()
	if !pollUntil(time.Now().Add(timeout), func() bool {
		ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 5*time.Second)
		resp, err := c.Version(ctx)
		cancel()
		if err != nil {
			t.Logf("waiting for talos API: %v", err)
			return false
		}
		tag := ""
		for _, msg := range resp.Messages {
			if msg.Version != nil {
				tag = msg.Version.Tag
			}
		}
		t.Logf("talos API ready: %s", tag)
		return true
	}) {
		t.Fatalf("talos API not reachable after %v", timeout)
	}
}

// PollKubeSpanStatus polls the Talos COSI API for KubeSpan peer status.
// Returns when at least minPeers peers are found and all are in "up" state.
func PollKubeSpanStatus(t *testing.T, c *client.Client, nodeIP string, timeout time.Duration) ([]KubespanPeerResult, error) {
	t.Helper()

	var lastErr string
	var finalPeers []KubespanPeerResult

	pollUntil(time.Now().Add(timeout), func() bool {
		ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 10*time.Second)
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, c.COSI)
		cancel()
		if err != nil {
			lastErr = err.Error()
			t.Logf("COSI poll (waiting): %s", lastErr)
			return false
		}

		var peers []KubespanPeerResult
		for it := list.Iterator(); it.Next(); {
			ps := it.Value()
			peers = append(peers, KubespanPeerResult{
				Label:    ps.TypedSpec().Label,
				State:    ps.TypedSpec().State,
				Endpoint: ps.TypedSpec().Endpoint.String(),
			})
		}

		allUp := len(peers) >= 2
		for _, p := range peers {
			if p.State != kubespan.PeerStateUp {
				allUp = false
			}
		}
		var peerSummary strings.Builder
		for i, p := range peers {
			if i > 0 {
				peerSummary.WriteString("; ")
			}
			fmt.Fprintf(&peerSummary, "%s state=%s ep=%s", p.Label, p.State, p.Endpoint)
		}
		t.Logf("COSI poll: %d peers, allUp=%v [%s]", len(peers), allUp, peerSummary.String())
		finalPeers = peers
		return allUp
	})

	allUp := len(finalPeers) >= 2
	for _, p := range finalPeers {
		if p.State != kubespan.PeerStateUp {
			allUp = false
		}
	}
	if allUp {
		return finalPeers, nil
	}
	return nil, fmt.Errorf("timeout after %v, last error: %s", timeout, lastErr)
}

// DumpKubeSpanDiagnostics queries COSI resources from a Talos node via the Talos
// API and logs them for debugging WireGuard handshake issues.
// Queries: kubespan.Identity, kubespan.Config, kubespan.PeerSpec, kubespan.PeerStatus,
// and cluster.Affiliate.
func DumpKubeSpanDiagnostics(t *testing.T, c *client.Client, nodeIP string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), nodeIP), 15*time.Second)
	defer cancel()

	t.Log("=== KubeSpan Diagnostics ===")

	// Local identity.
	if id, err := safe.StateGetByID[*kubespan.Identity](ctx, c.COSI, kubespan.LocalIdentity); err == nil {
		spec := id.TypedSpec()
		t.Logf("  Identity: pubkey=%s addr=%s subnet=%s", spec.PublicKey, spec.Address, spec.Subnet)
	} else {
		t.Logf("  Identity: error: %v", err)
	}

	// KubeSpan config.
	if cfg, err := safe.StateGetByID[*kubespan.Config](ctx, c.COSI, kubespan.ConfigID); err == nil {
		spec := cfg.TypedSpec()
		t.Logf("  Config: enabled=%v clusterID=%s forceRouting=%v mtu=%d filters=%v harvestExtra=%v",
			spec.Enabled, truncate(spec.ClusterID, 16), spec.ForceRouting, spec.MTU,
			spec.EndpointFilters, spec.HarvestExtraEndpoints)
	} else {
		t.Logf("  Config: error: %v", err)
	}

	// Affiliates (what the discovery service returned).
	if affiliates, err := safe.StateListAll[*cluster.Affiliate](ctx, c.COSI); err == nil {
		t.Logf("  Affiliates: %d total", affiliates.Len())
		for it := affiliates.Iterator(); it.Next(); {
			a := it.Value()
			spec := a.TypedSpec()
			var endpointStrs []string
			for _, ep := range spec.KubeSpan.Endpoints {
				endpointStrs = append(endpointStrs, ep.String())
			}
			t.Logf("    [%s] nodeID=%s hostname=%q type=%s addrs=%v ks_pubkey=%s ks_addr=%s ks_endpoints=[%s] ks_addl_addrs=%v",
				a.Metadata().ID(),
				truncate(spec.NodeID, 16),
				spec.Hostname,
				spec.MachineType,
				spec.Addresses,
				truncate(spec.KubeSpan.PublicKey, 16),
				spec.KubeSpan.Address,
				strings.Join(endpointStrs, ", "),
				spec.KubeSpan.AdditionalAddresses,
			)
		}
	} else {
		t.Logf("  Affiliates: error: %v", err)
	}

	// PeerSpecs (what the PeerSpecController produced from affiliates).
	if peers, err := safe.StateListAll[*kubespan.PeerSpec](ctx, c.COSI); err == nil {
		t.Logf("  PeerSpecs: %d total", peers.Len())
		for it := peers.Iterator(); it.Next(); {
			p := it.Value()
			spec := p.TypedSpec()
			var endpointStrs []string
			for _, ep := range spec.Endpoints {
				endpointStrs = append(endpointStrs, ep.String())
			}
			var allowedStrs []string
			for _, aip := range spec.AllowedIPs {
				allowedStrs = append(allowedStrs, aip.String())
			}
			t.Logf("    [%s] label=%q addr=%s endpoints=[%s] allowedIPs=[%s]",
				p.Metadata().ID(),
				spec.Label,
				spec.Address,
				strings.Join(endpointStrs, ", "),
				strings.Join(allowedStrs, ", "),
			)
		}
	} else {
		t.Logf("  PeerSpecs: error: %v", err)
	}

	// PeerStatuses (current WG handshake state).
	if statuses, err := safe.StateListAll[*kubespan.PeerStatus](ctx, c.COSI); err == nil {
		t.Logf("  PeerStatuses: %d total", statuses.Len())
		for it := statuses.Iterator(); it.Next(); {
			s := it.Value()
			spec := s.TypedSpec()
			t.Logf("    [%s] label=%q state=%s endpoint=%s lastHandshake=%s rx=%d tx=%d lastUsed=%s",
				s.Metadata().ID(),
				spec.Label,
				spec.State,
				spec.Endpoint,
				spec.LastHandshakeTime.Format(time.RFC3339),
				spec.ReceiveBytes,
				spec.TransmitBytes,
				spec.LastUsedEndpoint,
			)
		}
	} else {
		t.Logf("  PeerStatuses: error: %v", err)
	}

	t.Log("=== End KubeSpan Diagnostics ===")
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
