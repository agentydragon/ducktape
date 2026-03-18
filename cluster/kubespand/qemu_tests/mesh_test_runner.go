// MeshTestRunner implements a convergence-based integration test framework for
// kubespand QEMU tests. It watches COSI resources on all nodes, streams dmesg,
// fires connectivity probes when peers come up, and exits when all success
// conditions are met — or when the test times out.
package qemu_tests

import (
	"bufio"
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
)

// MeshTestRunner runs a convergence-based integration test.
// It watches COSI resources on all nodes, streams dmesg, fires probes
// when peers come up, and exits when all success conditions are met.
type MeshTestRunner struct {
	T         *testing.T
	Nodes     []*MeshNode
	Stopwatch *Stopwatch
	OutDir    string

	// SuccessFunc returns true when the test should pass.
	SuccessFunc func(state *MeshState) bool

	// ProbeTargets returns probe specs to fire when a node's peers are all "up".
	// nil means no probes.
	ProbeTargets func(node *MeshNode, peerSpecs map[string]kubespan.PeerSpecSpec) []ProbeSpec
}

// ProbeSpec describes a single connectivity probe to fire.
type ProbeSpec struct {
	Type   string // "icmp" or "tcp"
	Target string // IP or ULA address
	Port   int    // TCP port (ignored for ICMP)
}

// NodeState tracks the observed COSI + probe state for a single node.
type NodeState struct {
	Ready        bool
	PeerStatuses map[string]kubespan.PeerStatusSpec
	PeerSpecs    map[string]kubespan.PeerSpecSpec
	Affiliates   map[string]cluster.AffiliateSpec
	HasIdentity  bool
	ProbeResults map[string]bool // "icmp:target" or "tcp:target:port" -> passed
	probesFired  bool            // tracks whether probes have been fired for this node
}

// MeshState tracks the observed state across all nodes.
type MeshState struct {
	Nodes map[string]*NodeState
}

// Summary returns a human-readable summary of the current state.
func (s *MeshState) Summary() string {
	var b strings.Builder
	for name, ns := range s.Nodes {
		up := countUp(ns.PeerStatuses)
		fmt.Fprintf(&b, "  %s: ready=%v identity=%v peers=%d(up=%d) affiliates=%d probes=%d",
			name, ns.Ready, ns.HasIdentity, len(ns.PeerStatuses), up, len(ns.Affiliates), len(ns.ProbeResults))
		for k, v := range ns.ProbeResults {
			fmt.Fprintf(&b, " %s=%v", k, v)
		}
		b.WriteByte('\n')
	}
	return b.String()
}

// cosiEvent wraps a COSI state.Event with source node name and resource type.
type cosiEvent struct {
	NodeName     string
	ResourceType resource.Type
	Event        state.Event
}

// dmesgLine wraps a dmesg line with source node name.
type dmesgLine struct {
	NodeName string
	Line     string
}

// probeResult reports the outcome of a single probe.
type probeResult struct {
	NodeName string
	Key      string // "icmp:target" or "tcp:target:port"
	OK       bool
}

// nodeReady signals that a node's COSI watches are established.
type nodeReady struct {
	NodeName string
	Err      error
}

// watchSpec describes a COSI resource type to watch.
type watchSpec struct {
	Namespace    string
	ResourceType resource.Type
}

var defaultWatches = []watchSpec{
	{kubespan.NamespaceName, kubespan.IdentityType},
	{kubespan.NamespaceName, kubespan.PeerSpecType},
	{kubespan.NamespaceName, kubespan.PeerStatusType},
	{kubespan.NamespaceName, kubespan.EndpointType},
	{cluster.NamespaceName, cluster.AffiliateType},
}

// Run blocks until SuccessFunc returns true or ctx is cancelled.
func (r *MeshTestRunner) Run(ctx context.Context) {
	r.T.Helper()

	meshState := &MeshState{Nodes: make(map[string]*NodeState, len(r.Nodes))}
	for _, n := range r.Nodes {
		meshState.Nodes[n.VM.Name] = &NodeState{
			PeerStatuses: make(map[string]kubespan.PeerStatusSpec),
			PeerSpecs:    make(map[string]kubespan.PeerSpecSpec),
			Affiliates:   make(map[string]cluster.AffiliateSpec),
			ProbeResults: make(map[string]bool),
		}
	}

	eventCh := make(chan cosiEvent, 64)
	dmesgCh := make(chan dmesgLine, 256)
	probeCh := make(chan probeResult, 32)
	readyCh := make(chan nodeReady, len(r.Nodes))

	// Launch per-node goroutines.
	for _, node := range r.Nodes {
		go r.watchNode(ctx, node, eventCh, dmesgCh, readyCh)
	}

	// Register cleanup.
	r.T.Cleanup(func() {
		DumpAllDiagnostics(r.T, r.Nodes)
		r.Stopwatch.Lap("diagnostics")
		r.Stopwatch.Summary(r.OutDir)
	})

	// Main event loop.
	for {
		select {
		case <-ctx.Done():
			r.T.Logf("=== timeout: final state ===\n%s", meshState.Summary())
			r.T.Fatalf("test timed out waiting for success conditions")
			return

		case nr := <-readyCh:
			if nr.Err != nil {
				r.T.Logf("[%s] failed to become ready: %v", nr.NodeName, nr.Err)
				continue
			}
			ns := meshState.Nodes[nr.NodeName]
			ns.Ready = true
			r.T.Logf("[%s] COSI watches established", nr.NodeName)
			r.Stopwatch.Lap(nr.NodeName + " ready")
			if r.SuccessFunc(meshState) {
				r.Stopwatch.Lap("success")
				return
			}

		case ev := <-eventCh:
			r.handleCOSIEvent(ctx, meshState, ev, probeCh)
			if r.SuccessFunc(meshState) {
				r.Stopwatch.Lap("success")
				return
			}

		case dl := <-dmesgCh:
			r.T.Logf("[%s] dmesg: %s", dl.NodeName, dl.Line)

		case pr := <-probeCh:
			ns := meshState.Nodes[pr.NodeName]
			ns.ProbeResults[pr.Key] = pr.OK
			r.T.Logf("[%s] probe: %+v", pr.NodeName, pr)
			if r.SuccessFunc(meshState) {
				r.Stopwatch.Lap("success")
				return
			}
		}
	}
}

// watchNode waits for a node to become reachable, then opens COSI watches and
// dmesg streaming. Runs in its own goroutine.
func (r *MeshTestRunner) watchNode(ctx context.Context, node *MeshNode, eventCh chan<- cosiEvent, dmesgCh chan<- dmesgLine, readyCh chan<- nodeReady) {
	name := node.VM.Name

	// Poll until reachable.
	for {
		select {
		case <-ctx.Done():
			readyCh <- nodeReady{NodeName: name, Err: ctx.Err()}
			return
		default:
		}

		rctx, cancel := context.WithTimeout(client.WithNode(ctx, node.NodeIP), 5*time.Second)
		_, err := node.TalosClient.Version(rctx)
		cancel()
		if err == nil {
			break
		}
		time.Sleep(1 * time.Second)
	}

	st, cleanup, err := node.cosiState()
	if err != nil {
		readyCh <- nodeReady{NodeName: name, Err: err}
		return
	}
	defer cleanup()

	nctx := client.WithNode(ctx, node.NodeIP)

	// Open COSI watches.
	for _, ws := range defaultWatches {
		ws := ws
		ch := make(chan state.Event, 16)
		kind := resource.NewMetadata(ws.Namespace, ws.ResourceType, "", resource.VersionUndefined)
		if err := st.WatchKind(nctx, kind, ch, state.WithBootstrapContents(true)); err != nil {
			r.T.Logf("[%s] WatchKind %s: %v", name, ws.ResourceType, err)
			continue
		}
		go func() {
			for {
				select {
				case <-ctx.Done():
					return
				case ev := <-ch:
					eventCh <- cosiEvent{
						NodeName:     name,
						ResourceType: ws.ResourceType,
						Event:        ev,
					}
				}
			}
		}()
	}

	// Stream dmesg.
	go r.streamDmesg(ctx, node, dmesgCh)

	readyCh <- nodeReady{NodeName: name}
}

// streamDmesg opens a follow=true dmesg stream and fans lines into dmesgCh.
func (r *MeshTestRunner) streamDmesg(ctx context.Context, node *MeshNode, dmesgCh chan<- dmesgLine) {
	name := node.VM.Name
	dctx := client.WithNode(ctx, node.NodeIP)

	stream, err := node.TalosClient.Dmesg(dctx, true, true)
	if err != nil {
		r.T.Logf("[%s] dmesg stream error: %v", name, err)
		return
	}

	reader, err := client.ReadStream(stream)
	if err != nil {
		r.T.Logf("[%s] dmesg ReadStream error: %v", name, err)
		return
	}
	defer reader.Close()

	scanner := bufio.NewScanner(reader)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		select {
		case dmesgCh <- dmesgLine{NodeName: name, Line: line}:
		case <-ctx.Done():
			return
		}
	}
	if err := scanner.Err(); err != nil && ctx.Err() == nil {
		r.T.Logf("[%s] dmesg scanner error: %v", name, err)
	}
}

// handleCOSIEvent processes a single COSI event, updates state, and may trigger probes.
func (r *MeshTestRunner) handleCOSIEvent(ctx context.Context, meshState *MeshState, ev cosiEvent, probeCh chan<- probeResult) {
	ns := meshState.Nodes[ev.NodeName]

	switch ev.Event.Type {
	case state.Created, state.Updated:
		res := ev.Event.Resource
		if res == nil {
			return
		}
		id := res.Metadata().ID()

		switch ev.ResourceType {
		case kubespan.PeerStatusType:
			ps, ok := res.(*kubespan.PeerStatus)
			if !ok {
				return
			}
			spec := *ps.TypedSpec()
			if ev.Event.Type == state.Updated {
				if old, exists := ns.PeerStatuses[id]; exists && old.State != spec.State {
					r.T.Logf("[%s] PeerStatus %s -> %s: %+v",
						ev.NodeName, old.State, spec.State, spec)
				}
			} else {
				r.T.Logf("[%s] PeerStatus created: %+v", ev.NodeName, spec)
			}
			ns.PeerStatuses[id] = spec

		case kubespan.PeerSpecType:
			ps, ok := res.(*kubespan.PeerSpec)
			if !ok {
				return
			}
			spec := *ps.TypedSpec()
			r.T.Logf("[%s] PeerSpec %s: %s %+v", ev.NodeName, ev.Event.Type, id, spec)
			ns.PeerSpecs[id] = spec

		case cluster.AffiliateType:
			aff, ok := res.(*cluster.Affiliate)
			if !ok {
				return
			}
			spec := *aff.TypedSpec()
			r.T.Logf("[%s] Affiliate %s: %+v", ev.NodeName, ev.Event.Type, spec)
			ns.Affiliates[id] = spec

		case kubespan.IdentityType:
			r.T.Logf("[%s] Identity %s: %s %+v", ev.NodeName, ev.Event.Type, id, res)
			ns.HasIdentity = true

		case kubespan.EndpointType:
			r.T.Logf("[%s] Endpoint %s: %s %+v", ev.NodeName, ev.Event.Type, id, res)
		}

		// Check if we should fire probes: all peers on this node are "up".
		r.maybeFireProbes(ctx, ev.NodeName, ns, probeCh)

	case state.Destroyed:
		res := ev.Event.Resource
		if res == nil {
			return
		}
		id := res.Metadata().ID()
		r.T.Logf("[%s] %s destroyed: %s", ev.NodeName, ev.ResourceType, id)

		switch ev.ResourceType {
		case kubespan.PeerStatusType:
			delete(ns.PeerStatuses, id)
		case kubespan.PeerSpecType:
			delete(ns.PeerSpecs, id)
		case cluster.AffiliateType:
			delete(ns.Affiliates, id)
		case kubespan.IdentityType:
			ns.HasIdentity = false
		}

	case state.Bootstrapped:
		r.T.Logf("[%s] %s bootstrap complete", ev.NodeName, ev.ResourceType)

	case state.Errored:
		if ev.Event.Error != nil {
			r.T.Logf("[%s] %s watch error: %v", ev.NodeName, ev.ResourceType, ev.Event.Error)
		}
	}
}

// maybeFireProbes checks if all peers are "up" on a node and fires probes once.
func (r *MeshTestRunner) maybeFireProbes(ctx context.Context, nodeName string, ns *NodeState, probeCh chan<- probeResult) {
	if r.ProbeTargets == nil || ns.probesFired {
		return
	}

	// Need at least one peer and all must be "up".
	if len(ns.PeerStatuses) == 0 {
		return
	}
	for _, ps := range ns.PeerStatuses {
		if ps.State != kubespan.PeerStateUp {
			return
		}
	}

	// Find the MeshNode for this name.
	var node *MeshNode
	for _, n := range r.Nodes {
		if n.VM.Name == nodeName {
			node = n
			break
		}
	}
	if node == nil {
		return
	}

	probes := r.ProbeTargets(node, ns.PeerSpecs)
	if len(probes) == 0 {
		return
	}

	ns.probesFired = true
	r.T.Logf("[%s] all peers up, firing %d probes", nodeName, len(probes))
	r.Stopwatch.Lap(nodeName + " full mesh")

	for _, p := range probes {
		p := p
		// Pre-populate as false so SuccessFunc knows these probes exist.
		key := probeKey(p)
		ns.ProbeResults[key] = false

		go func() {
			var ok bool
			switch p.Type {
			case "icmp":
				ok = node.ProbeICMP(p.Target, 1*time.Second)
			case "tcp":
				ok = node.ProbeTCP(p.Target, p.Port, 1*time.Second)
			}
			select {
			case probeCh <- probeResult{NodeName: nodeName, Key: key, OK: ok}:
			case <-ctx.Done():
			}
		}()
	}
}

func probeKey(p ProbeSpec) string {
	if p.Type == "tcp" {
		return fmt.Sprintf("tcp:%s:%d", p.Target, p.Port)
	}
	return fmt.Sprintf("icmp:%s", p.Target)
}

// FullMeshSuccess returns a SuccessFunc that requires all nodes ready,
// all peers "up" on every node, and all probes passed.
func FullMeshSuccess(expectedPeers int) func(*MeshState) bool {
	return func(s *MeshState) bool {
		for _, ns := range s.Nodes {
			if !ns.Ready {
				return false
			}
			if countUp(ns.PeerStatuses) < expectedPeers {
				return false
			}
			for _, ok := range ns.ProbeResults {
				if !ok {
					return false
				}
			}
		}
		return true
	}
}

// WorkerAPISuccess returns a SuccessFunc that passes when the named worker
// node's COSI watches are established (proving the trustd CSR flow succeeded).
func WorkerAPISuccess(workerNodeName string) func(*MeshState) bool {
	return func(s *MeshState) bool {
		ns, ok := s.Nodes[workerNodeName]
		return ok && ns.Ready
	}
}

// ULAProbeTargets returns probe specs for all peer ULA addresses (ICMP + TCP:9999).
// Only fires for nodes with a probe server (kubespand nodes).
func ULAProbeTargets(node *MeshNode, peerSpecs map[string]kubespan.PeerSpecSpec) []ProbeSpec {
	if !node.HasProbeServer() {
		return nil
	}
	var probes []ProbeSpec
	for _, ps := range peerSpecs {
		addr := ps.Address.String()
		probes = append(probes, ProbeSpec{Type: "icmp", Target: addr})
		probes = append(probes, ProbeSpec{Type: "tcp", Target: addr, Port: 9999})
	}
	return probes
}
