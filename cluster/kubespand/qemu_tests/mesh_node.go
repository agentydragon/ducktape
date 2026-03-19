// MeshNode abstracts over Talos and kubespand VMs participating in the KubeSpan
// mesh. Both expose COSI state and MachineService RPCs via the Talos mTLS API
// (kubespand via its apid subprocess, Talos natively).
// MeshNode provides a unified interface for mesh verification and test probes.
package qemu_tests

import (
	"context"
	"errors"
	"fmt"
	"io"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/api/machine"
	"github.com/siderolabs/talos/pkg/machinery/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
)

// NodeType identifies whether a mesh node runs Talos or kubespand.
type NodeType string

const (
	NodeTypeTalos     NodeType = "talos"
	NodeTypeKubespand NodeType = "kubespand"
)

// Standard test timeouts. Tests use these instead of ad-hoc durations.
const (
	// NodeReadyTimeout is how long to wait for a node's API to become reachable.
	NodeReadyTimeout = 180 * time.Second
	// FullMeshTimeout is how long to wait for full mesh convergence after nodes are ready.
	FullMeshTimeout = 30 * time.Second
)

// MeshNode wraps a VM with node-type-specific COSI access and probe
// capabilities. For kubespand nodes, COSI is accessed via direct insecure
// gRPC; for Talos nodes, via the Talos mTLS API client.
type MeshNode struct {
	VM          *VM
	Type        NodeType
	NodeIP      string // Talos-only: node IP for WithNode context
	T           *testing.T
	TalosClient *client.Client // nil for kubespand
}

// NewTalosMeshNode constructs a MeshNode for a Talos VM, creating the Talos
// mTLS API client internally. The client is closed automatically via t.Cleanup.
//
// Uses the management NIC IP (10.0.2.15) for client.WithNode context. Each
// TalosClient already connects to the correct node via port forwarding, so
// WithNode just needs to resolve to "this node" inside apid. The management IP
// is statically assigned in the Talos config (eth1), available immediately on
// boot without depending on COSI NodeAddress reconciliation. Using mesh-facing
// IPs (e.g. 192.168.60.2) fails in NAT topologies because apid can't verify
// them as local during KubeSpan network reconfiguration.
func NewTalosMeshNode(t *testing.T, vm *VM, talosConfigPath string, apiPort int) *MeshNode {
	t.Helper()
	c := NewTalosClient(t, talosConfigPath, fmt.Sprintf("127.0.0.1:%d", apiPort))
	t.Cleanup(func() { c.Close() })
	return &MeshNode{VM: vm, Type: NodeTypeTalos, NodeIP: MgmtIP, T: t, TalosClient: c}
}

// Close cleans up resources (closes the Talos client if present).
func (w *MeshNode) Close() {
	if w.TalosClient != nil {
		w.TalosClient.Close()
	}
}

// Logf logs a message prefixed with the node name.
func (w *MeshNode) Logf(format string, args ...interface{}) {
	w.T.Helper()
	w.T.Logf("[%s] "+format, append([]interface{}{w.VM.Name}, args...)...)
}

// waitForReady waits for the node to become reachable. Returns an error instead
// of calling t.Fatalf, so it's safe to call from goroutines.
func (w *MeshNode) waitForReady(timeout time.Duration) error {
	switch w.Type {
	case NodeTypeKubespand:
		if !w.VM.WaitForProbeServer(timeout) {
			return fmt.Errorf("probe server not ready after %v", timeout)
		}
		return nil
	case NodeTypeTalos:
		if !pollUntil(time.Now().Add(timeout), func() bool {
			ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), w.NodeIP), 5*time.Second)
			_, err := w.TalosClient.Version(ctx)
			cancel()
			return err == nil
		}) {
			return fmt.Errorf("talos API not reachable after %v", timeout)
		}
		return nil
	default:
		return fmt.Errorf("unknown node type %q", w.Type)
	}
}

// WaitForNodesReady waits for all nodes to become reachable in parallel.
// Collects readiness results in goroutines and fails from the test goroutine
// to avoid panics from t.Fatalf in non-test goroutines.
func WaitForNodesReady(t *testing.T, nodes []*MeshNode, timeout time.Duration) {
	t.Helper()

	type result struct {
		name string
		err  error
	}
	ch := make(chan result, len(nodes))
	for _, w := range nodes {
		w := w
		go func() {
			err := w.waitForReady(timeout)
			ch <- result{name: w.VM.Name, err: err}
		}()
	}
	for range len(nodes) {
		r := <-ch
		if r.err != nil {
			t.Fatalf("[%s] not ready: %v", r.name, r.err)
		}
	}
}

// HasProbeServer returns true for kubespand nodes (which run a gRPC probe
// server for ICMP/TCP connectivity tests). Talos nodes lack this.
func (w *MeshNode) HasProbeServer() bool {
	return w.Type == NodeTypeKubespand
}

// GetPeerStatuses fetches all PeerStatus resources in a single call.
func (w *MeshNode) GetPeerStatuses() ([]kubespan.PeerStatusSpec, error) {
	st, cleanup, err := w.cosiState()
	if err != nil {
		return nil, err
	}
	defer cleanup()

	ctx, cancel := context.WithTimeout(w.nodeContext(), 10*time.Second)
	defer cancel()

	list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, st)
	if err != nil {
		return nil, err
	}

	resources := collectList(list)
	peers := make([]kubespan.PeerStatusSpec, len(resources))
	for i, r := range resources {
		peers[i] = *r.TypedSpec()
	}
	return peers, nil
}

// GetPeerSpecs queries all PeerSpec resources from the worker's COSI API.
func (w *MeshNode) GetPeerSpecs() ([]kubespan.PeerSpecSpec, error) {
	w.T.Helper()

	st, cleanup, err := w.cosiState()
	if err != nil {
		return nil, err
	}
	defer cleanup()

	ctx, cancel := context.WithTimeout(w.nodeContext(), 10*time.Second)
	defer cancel()

	list, err := safe.StateListAll[*kubespan.PeerSpec](ctx, st)
	if err != nil {
		return nil, fmt.Errorf("COSI list PeerSpec: %w", err)
	}

	resources := collectList(list)
	result := make([]kubespan.PeerSpecSpec, len(resources))
	for i, r := range resources {
		result[i] = *r.TypedSpec()
	}
	return result, nil
}

// ProbeICMP sends an ICMP probe from the node's VM. Only works on kubespand
// nodes (skips with true for Talos nodes that lack a probe server).
func (w *MeshNode) ProbeICMP(target string, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.Logf("skipping ICMP probe (Talos node, no probe server)")
		return true // not a failure, just not testable
	}
	return w.VM.ProbeICMP(target, timeout)
}

// ProbeTCP sends a TCP probe from the node's VM. Only works on kubespand
// nodes (skips with true for Talos nodes that lack a probe server).
func (w *MeshNode) ProbeTCP(target string, port int, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.Logf("skipping TCP probe (Talos node, no probe server)")
		return true
	}
	return w.VM.ProbeTCP(target, port, timeout)
}

// DumpDiagnostics logs diagnostic information from the node.
// Both node types dump COSI KubeSpan state via the Talos mTLS API.
// Kubespand nodes additionally dump routing/nft diagnostics from the probe server.
func (w *MeshNode) DumpDiagnostics(t *testing.T) {
	t.Helper()
	w.Logf("=== diagnostics (%s) ===", w.Type)

	// COSI KubeSpan state (works for both node types via Talos mTLS API).
	if w.TalosClient != nil {
		DumpKubeSpanDiagnostics(t, w.TalosClient, w.NodeIP)
	}

	// Routing/WG/nftables state from probe server (kubespand only — these are
	// subprocess commands with no MachineService equivalent).
	if w.HasProbeServer() {
		w.Logf("probe server diagnostics:")
		t.Log(w.VM.DumpDiagnostics())
	}

	w.Logf("=== end diagnostics ===")
}

// DumpDmesg logs kernel messages from the node via the Talos MachineService
// Dmesg() RPC. Both kubespand and Talos nodes serve this RPC.
func (w *MeshNode) DumpDmesg(t *testing.T) {
	t.Helper()

	if w.TalosClient == nil {
		w.Logf("dmesg: no Talos client")
		return
	}

	ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), w.NodeIP), 10*time.Second)
	defer cancel()

	stream, err := w.TalosClient.Dmesg(ctx, false, false)
	if err != nil {
		w.Logf("dmesg error: %v", err)
		return
	}

	reader, err := client.ReadStream(stream)
	if err != nil {
		w.Logf("dmesg read stream error: %v", err)
		return
	}
	defer reader.Close()

	data, err := io.ReadAll(reader)
	if err != nil {
		w.Logf("dmesg read error: %v", err)
		return
	}

	w.Logf("dmesg (%d bytes):\n%s", len(data), string(data))
}

// DumpNetstat logs network socket information from the node via the Talos
// MachineService Netstat() RPC. Both kubespand and Talos nodes serve this RPC.
func (w *MeshNode) DumpNetstat(t *testing.T) {
	t.Helper()

	if w.TalosClient == nil {
		w.Logf("netstat: no Talos client")
		return
	}

	ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), w.NodeIP), 10*time.Second)
	defer cancel()

	resp, err := w.TalosClient.Netstat(ctx, &machine.NetstatRequest{
		Filter: machine.NetstatRequest_LISTENING,
		L4Proto: &machine.NetstatRequest_L4Proto{
			Tcp:  true,
			Tcp6: true,
			Udp:  true,
			Udp6: true,
		},
	})
	if err != nil {
		w.Logf("netstat error: %v", err)
		return
	}

	for _, msg := range resp.Messages {
		for _, rec := range msg.Connectrecord {
			w.Logf("netstat: %+v", rec)
		}
	}
}

// DumpMemoryStats logs memory usage from the node via the Talos MachineService
// Memory() RPC.
func (w *MeshNode) DumpMemoryStats(t *testing.T) {
	t.Helper()

	if w.TalosClient == nil {
		w.Logf("memory stats: no Talos client")
		return
	}
	ctx, cancel := context.WithTimeout(client.WithNode(context.Background(), w.NodeIP), 10*time.Second)
	defer cancel()
	resp, err := w.TalosClient.Memory(ctx)
	if err != nil {
		w.Logf("memory stats error: %v", err)
		return
	}
	for _, msg := range resp.Messages {
		if msg.Meminfo != nil {
			w.Logf("memory: %+v", msg.Meminfo)
		}
	}
}

// DumpAllDiagnostics dumps comprehensive diagnostics for all nodes:
// memory stats, dmesg, and netstat. Called at the end of every test.
func DumpAllDiagnostics(t *testing.T, nodes []*MeshNode) {
	t.Helper()
	for _, n := range nodes {
		n.DumpMemoryStats(t)
		n.DumpDmesg(t)
		n.DumpNetstat(t)
	}
}

// cosiState returns a COSI state.State client via the Talos mTLS API.
// Both kubespand (via apid) and Talos nodes expose COSI through this path.
// cleanup must be called when done.
func (w *MeshNode) cosiState() (state.State, func(), error) {
	if w.TalosClient == nil {
		return nil, nil, fmt.Errorf("[%s] Talos client not configured", w.VM.Name)
	}
	return w.TalosClient.COSI, func() {}, nil
}

// nodeContext returns a context with client.WithNode set for the target node.
func (w *MeshNode) nodeContext() context.Context {
	ctx := context.Background()
	if w.NodeIP != "" {
		ctx = client.WithNode(ctx, w.NodeIP)
	}
	return ctx
}

// WaitForFullMesh watches PeerStatus on all nodes via COSI WatchKind and
// returns when every node reports all other nodes as "up". Logs every peer
// state transition for visibility.
func WaitForFullMesh(t *testing.T, nodes []*MeshNode, timeout time.Duration) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	expected := len(nodes) - 1

	type nodeResult struct {
		node  *MeshNode
		peers map[string]kubespan.PeerStatusSpec
		err   error
	}
	results := make(chan nodeResult, len(nodes))

	for _, node := range nodes {
		node := node
		go func() {
			peers, err := watchNodeMesh(ctx, t, node, expected)
			results <- nodeResult{node, peers, err}
		}()
	}

	var errs []error
	for range nodes {
		r := <-results
		if r.err != nil {
			for label, p := range r.peers {
				t.Logf("[%s] final: %s state=%s ep=%s", r.node.VM.Name, label, p.State, p.Endpoint)
			}
			errs = append(errs, fmt.Errorf("%s: %w", r.node.VM.Name, r.err))
		} else {
			t.Logf("[%s] full mesh achieved (%d peers)", r.node.VM.Name, len(r.peers))
		}
	}
	return errors.Join(errs...)
}

// watchNodeMesh uses COSI WatchKind to stream PeerStatus events from a single
// node. Returns when all expected peers are "up" or ctx is cancelled.
func watchNodeMesh(ctx context.Context, t *testing.T, node *MeshNode, expected int) (map[string]kubespan.PeerStatusSpec, error) {
	t.Helper()

	st, cleanup, err := node.cosiState()
	if err != nil {
		return nil, err
	}
	defer cleanup()

	ch := make(chan state.Event)
	nctx := node.nodeContext()
	nctx, ncancel := context.WithCancel(nctx)
	defer ncancel()

	// Create a resource kind reference for PeerStatus.
	kind := resource.NewMetadata(kubespan.NamespaceName, kubespan.PeerStatusType, "", resource.VersionUndefined)

	// WatchKind with bootstrap to get initial state as Created events.
	if err := st.WatchKind(nctx, kind, ch, state.WithBootstrapContents(true)); err != nil {
		return nil, fmt.Errorf("WatchKind: %w", err)
	}

	peers := map[string]kubespan.PeerStatusSpec{}
	for {
		select {
		case <-ctx.Done():
			return peers, fmt.Errorf("timeout waiting for %d peers up (have %d up)", expected, countUp(peers))
		case ev := <-ch:
			switch ev.Type {
			case state.Created:
				ps, ok := ev.Resource.(*kubespan.PeerStatus)
				if !ok {
					continue
				}
				spec := *ps.TypedSpec()
				t.Logf("[%s] peer created: %s state=%s ep=%s", node.VM.Name, spec.Label, spec.State, spec.Endpoint)
				peers[ev.Resource.Metadata().ID()] = spec

			case state.Updated:
				ps, ok := ev.Resource.(*kubespan.PeerStatus)
				if !ok {
					continue
				}
				spec := *ps.TypedSpec()
				id := ev.Resource.Metadata().ID()
				old, exists := peers[id]
				if exists && old.State != spec.State {
					t.Logf("[%s] peer transition: %s %s -> %s ep=%s",
						node.VM.Name, spec.Label, old.State, spec.State, spec.Endpoint)
				} else if exists && old.Endpoint != spec.Endpoint {
					t.Logf("[%s] peer endpoint change: %s ep=%s -> %s",
						node.VM.Name, spec.Label, old.Endpoint, spec.Endpoint)
				}
				peers[id] = spec

			case state.Destroyed:
				id := ev.Resource.Metadata().ID()
				if old, exists := peers[id]; exists {
					t.Logf("[%s] peer destroyed: %s (was %s)", node.VM.Name, old.Label, old.State)
				}
				delete(peers, id)

			case state.Bootstrapped:
				t.Logf("[%s] bootstrap complete: %d peers", node.VM.Name, len(peers))

			case state.Errored:
				if ev.Error != nil {
					t.Logf("[%s] watch error: %v", node.VM.Name, ev.Error)
				}
			}

			up := countUp(peers)
			if up >= expected {
				return peers, nil
			}
		}
	}
}

func countUp(peers map[string]kubespan.PeerStatusSpec) int {
	n := 0
	for _, p := range peers {
		if p.State == kubespan.PeerStateUp {
			n++
		}
	}
	return n
}
