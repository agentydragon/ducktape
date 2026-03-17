// WorkerNode abstracts over Talos and kubespand worker VMs for parameterized
// topology tests. Both expose COSI state (PeerStatus, PeerSpec) but via
// different transports: Talos via its mTLS API client, kubespand via direct
// insecure gRPC. The WorkerNode provides a unified interface for test assertions.
package qemu_tests

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"
	"github.com/siderolabs/talos/pkg/machinery/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// WorkerType identifies whether a worker VM runs Talos or kubespand.
type WorkerType string

const (
	WorkerTypeTalos     WorkerType = "talos"
	WorkerTypeKubespand WorkerType = "kubespand"
)

// WorkerNode wraps a VM with worker-type-specific COSI access and probe
// capabilities. For kubespand workers, COSI is accessed via direct insecure
// gRPC; for Talos workers, via the Talos mTLS API client.
type WorkerNode struct {
	VM          *VM
	Type        WorkerType
	NodeIP      string // Talos-only: node IP for WithNode context
	T           *testing.T
	TalosClient *client.Client // nil for kubespand
}

// Close cleans up resources (closes the Talos client if present).
func (w *WorkerNode) Close() {
	if w.TalosClient != nil {
		w.TalosClient.Close()
	}
}

// WaitForReady waits for the worker to become reachable. For kubespand workers,
// waits for the probe server. For Talos workers, waits for the Talos API.
func (w *WorkerNode) WaitForReady(timeout time.Duration) {
	w.T.Helper()
	switch w.Type {
	case WorkerTypeKubespand:
		WaitForProbeServers(w.T, []*VM{w.VM}, timeout)
	case WorkerTypeTalos:
		WaitForTalosAPI(w.T, w.TalosClient, w.NodeIP, timeout)
	}
}

// WaitForWorkersReady waits for all workers to become ready in parallel.
func WaitForWorkersReady(t *testing.T, workers []*WorkerNode, timeout time.Duration) {
	t.Helper()

	done := make(chan struct{}, len(workers))
	for _, w := range workers {
		w := w
		go func() {
			w.WaitForReady(timeout)
			done <- struct{}{}
		}()
	}
	for range len(workers) {
		<-done
	}
}

// HasProbeServer returns true for kubespand workers (which run a gRPC probe
// server for ICMP/TCP connectivity tests). Talos workers lack this.
func (w *WorkerNode) HasProbeServer() bool {
	return w.Type == WorkerTypeKubespand
}

// PollPeerStatus polls COSI PeerStatus resources until at least minPeers
// report state "up", or timeout is reached.
func (w *WorkerNode) PollPeerStatus(minPeers int, timeout time.Duration) ([]kubespan.PeerStatusSpec, error) {
	w.T.Helper()

	deadline := time.Now().Add(timeout)
	var lastErr string

	for time.Now().Before(deadline) {
		peers, err := w.listPeerStatuses()
		if err != nil {
			lastErr = err.Error()
			w.T.Logf("[%s] COSI poll (waiting): %s", w.VM.Name, lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		upCount := 0
		for _, p := range peers {
			if p.State == kubespan.PeerStateUp {
				upCount++
			}
		}

		var summary strings.Builder
		for i, p := range peers {
			if i > 0 {
				summary.WriteString("; ")
			}
			fmt.Fprintf(&summary, "%s state=%s ep=%s", p.Label, p.State, p.Endpoint)
		}
		w.T.Logf("[%s] COSI poll: %d peers, %d up (need %d) [%s]", w.VM.Name, len(peers), upCount, minPeers, summary.String())

		if upCount >= minPeers {
			return peers, nil
		}

		time.Sleep(1 * time.Second)
	}

	return nil, fmt.Errorf("timeout after %v waiting for %d peers up, last error: %s", timeout, minPeers, lastErr)
}

// GetPeerSpecs queries all PeerSpec resources from the worker's COSI API.
func (w *WorkerNode) GetPeerSpecs() ([]kubespan.PeerSpecSpec, error) {
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

// ProbeICMP sends an ICMP probe from the worker VM. Only works on kubespand
// workers (returns false for Talos).
func (w *WorkerNode) ProbeICMP(target string, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.T.Logf("[%s] skipping ICMP probe (Talos worker, no probe server)", w.VM.Name)
		return true // not a failure, just not testable
	}
	return w.VM.ProbeICMP(target, timeout)
}

// ProbeTCP sends a TCP probe from the worker VM. Only works on kubespand
// workers (returns false for Talos).
func (w *WorkerNode) ProbeTCP(target string, port int, timeout time.Duration) bool {
	if !w.HasProbeServer() {
		w.T.Logf("[%s] skipping TCP probe (Talos worker, no probe server)", w.VM.Name)
		return true
	}
	return w.VM.ProbeTCP(target, port, timeout)
}

// DumpDiagnostics logs diagnostic information from the worker.
func (w *WorkerNode) DumpDiagnostics(t *testing.T) {
	t.Helper()
	switch w.Type {
	case WorkerTypeKubespand:
		t.Logf("=== %s diagnostics (kubespand) ===", w.VM.Name)
		t.Log(w.VM.DumpDiagnostics())
	case WorkerTypeTalos:
		t.Logf("=== %s diagnostics (talos) ===", w.VM.Name)
		if w.TalosClient != nil {
			DumpKubeSpanDiagnostics(t, w.TalosClient, w.NodeIP)
		}
	}
}

// listPeerStatuses fetches all PeerStatus resources in a single call.
func (w *WorkerNode) listPeerStatuses() ([]kubespan.PeerStatusSpec, error) {
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

// cosiState returns a COSI state.State client appropriate for the worker type.
// For kubespand: insecure gRPC to vm.cosiAddr.
// For Talos: the client's COSI field.
// cleanup must be called when done.
func (w *WorkerNode) cosiState() (state.State, func(), error) {
	switch w.Type {
	case WorkerTypeKubespand:
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

	case WorkerTypeTalos:
		if w.TalosClient == nil {
			return nil, nil, fmt.Errorf("[%s] Talos client not configured", w.VM.Name)
		}
		return w.TalosClient.COSI, func() {}, nil

	default:
		return nil, nil, fmt.Errorf("unknown worker type: %s", w.Type)
	}
}

// nodeContext returns a context appropriate for the worker type.
// For Talos, wraps with client.WithNode to target the specific node.
func (w *WorkerNode) nodeContext() context.Context {
	ctx := context.Background()
	if w.Type == WorkerTypeTalos && w.NodeIP != "" {
		ctx = client.WithNode(ctx, w.NodeIP)
	}
	return ctx
}
