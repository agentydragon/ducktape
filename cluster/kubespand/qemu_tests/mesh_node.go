// MeshNode abstracts over Talos and kubespand VMs participating in the KubeSpan
// mesh. Both expose COSI state (PeerStatus, PeerSpec) but via different
// transports: Talos via its mTLS API client, kubespand via direct insecure gRPC.
// MeshNode provides a unified interface for mesh verification and test probes.
package qemu_tests

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"
	"github.com/siderolabs/talos/pkg/machinery/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
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
	FullMeshTimeout = 120 * time.Second
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

// Close cleans up resources (closes the Talos client if present).
func (w *MeshNode) Close() {
	if w.TalosClient != nil {
		w.TalosClient.Close()
	}
}

// WaitForReady waits for the node to become reachable. For kubespand nodes,
// waits for the probe server. For Talos nodes, waits for the Talos API.
func (w *MeshNode) WaitForReady(timeout time.Duration) {
	w.T.Helper()
	switch w.Type {
	case NodeTypeKubespand:
		WaitForProbeServers(w.T, []*VM{w.VM}, timeout)
	case NodeTypeTalos:
		WaitForTalosAPI(w.T, w.TalosClient, w.NodeIP, timeout)
	default:
		w.T.Fatalf("unknown node type %q in WaitForReady", w.Type)
	}
}

// WaitForNodesReady waits for all nodes to become reachable in parallel.
func WaitForNodesReady(t *testing.T, nodes []*MeshNode, timeout time.Duration) {
	t.Helper()

	done := make(chan struct{}, len(nodes))
	for _, w := range nodes {
		w := w
		go func() {
			w.WaitForReady(timeout)
			done <- struct{}{}
		}()
	}
	for range len(nodes) {
		<-done
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
// nodes (returns true without probing for Talos).
func (w *MeshNode) ProbeICMP(target string, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.T.Logf("[%s] skipping ICMP probe (Talos node, no probe server)", w.VM.Name)
		return true // not a failure, just not testable
	}
	return w.VM.ProbeICMP(target, timeout)
}

// ProbeTCP sends a TCP probe from the node's VM. Only works on kubespand
// nodes (returns true without probing for Talos).
func (w *MeshNode) ProbeTCP(target string, port int, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.T.Logf("[%s] skipping TCP probe (Talos node, no probe server)", w.VM.Name)
		return true
	}
	return w.VM.ProbeTCP(target, port, timeout)
}

// DumpDiagnostics logs diagnostic information from the node.
func (w *MeshNode) DumpDiagnostics(t *testing.T) {
	t.Helper()
	switch w.Type {
	case NodeTypeKubespand:
		t.Logf("=== %s diagnostics (kubespand) ===", w.VM.Name)
		t.Log(w.VM.DumpDiagnostics())
	case NodeTypeTalos:
		t.Logf("=== %s diagnostics (talos) ===", w.VM.Name)
		if w.TalosClient != nil {
			DumpKubeSpanDiagnostics(t, w.TalosClient, w.NodeIP)
		}
	}
}

// cosiState returns a COSI state.State client appropriate for the node type.
// For kubespand: insecure gRPC to vm.cosiAddr.
// For Talos: the client's COSI field.
// cleanup must be called when done.
func (w *MeshNode) cosiState() (state.State, func(), error) {
	switch w.Type {
	case NodeTypeKubespand:
		if w.VM.cosiAddr == "" {
			return nil, nil, fmt.Errorf("[%s] COSI not configured", w.VM.Name)
		}
		conn, err := grpc.NewClient(w.VM.cosiAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			return nil, nil, fmt.Errorf("COSI connect: %w", err)
		}
		st := state.WrapCore(stateclient.NewAdapter(v1alpha1.NewStateClient(conn)))
		return st, func() { conn.Close() }, nil

	case NodeTypeTalos:
		if w.TalosClient == nil {
			return nil, nil, fmt.Errorf("[%s] Talos client not configured", w.VM.Name)
		}
		return w.TalosClient.COSI, func() {}, nil

	default:
		return nil, nil, fmt.Errorf("unknown node type: %s", w.Type)
	}
}

// nodeContext returns a context appropriate for the node type.
// For Talos, wraps with client.WithNode to target the specific node.
func (w *MeshNode) nodeContext() context.Context {
	ctx := context.Background()
	if w.Type == NodeTypeTalos && w.NodeIP != "" {
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
