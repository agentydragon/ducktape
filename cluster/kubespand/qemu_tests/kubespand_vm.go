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
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	pb "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/probepb"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// ProbeServerGuestPort is the well-known port the probe gRPC server listens
// on inside the VM. Matches initlib.ProbeServerPort.
const ProbeServerGuestPort = 50200

// KubespandVM is a running Alpine VM with kubespand and a probe gRPC server.
// It wraps *VM with pre-configured host addresses for the probe and COSI APIs.
type KubespandVM struct {
	*VM
	t         *testing.T
	probeAddr string // "127.0.0.1:<hostPort>" for probe gRPC
	cosiAddr  string // "127.0.0.1:<hostPort>" for COSI API, empty if not configured
}

// BootKubespandVM boots an Alpine VM running kubespand with a probe server
// and optional COSI API port forwarding. The mgmt NIC is configured
// automatically with random host ports. netArgs should contain the mesh
// NIC(s) (e.g., McastNIC calls).
//
// If withCOSI is true, the COSI API (guest port 50100) is also forwarded
// and kernelArgs should include "listen_tcp=:50100".
func BootKubespandVM(t *testing.T, name, vmlinuz, initramfs, kernelArgs string, withCOSI bool, mac string, netArgs []string) *KubespandVM {
	t.Helper()

	probePort := RandomPort()
	kvm := &KubespandVM{
		t:         t,
		probeAddr: fmt.Sprintf("127.0.0.1:%d", probePort),
	}

	var mgmtArgs []string
	if withCOSI {
		cosiPort := RandomPort()
		kvm.cosiAddr = fmt.Sprintf("127.0.0.1:%d", cosiPort)
		mgmtArgs = MgmtNICMulti([]PortForward{
			{HostPort: cosiPort, GuestPort: 50100},
			{HostPort: probePort, GuestPort: ProbeServerGuestPort},
		}, mac)
	} else {
		mgmtArgs = MgmtNIC(probePort, ProbeServerGuestPort, mac)
	}

	allArgs := append(netArgs, mgmtArgs...)
	kvm.VM = BootVM(t, name, vmlinuz, initramfs, kernelArgs, allArgs...)
	return kvm
}

// ProbeICMP sends an ICMP probe request via the VM's probe server, retrying
// until success or timeout.
func (v *KubespandVM) ProbeICMP(target string, timeout time.Duration) bool {
	v.t.Helper()
	deadline := time.Now().Add(timeout)

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		conn, err := grpc.NewClient(v.probeAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			v.t.Logf("[%s] probe ICMP connect: %v", v.Name, err)
			time.Sleep(1 * time.Second)
			continue
		}

		client := pb.NewProbeServiceClient(conn)
		resp, err := client.ICMPProbe(ctx, &pb.ICMPProbeRequest{
			Target:         target,
			TimeoutSeconds: 10,
		})
		cancel()
		conn.Close()

		if err != nil {
			v.t.Logf("[%s] probe ICMP→%s: %v", v.Name, target, err)
			time.Sleep(1 * time.Second)
			continue
		}

		if resp.Success {
			v.t.Logf("[%s] probe ICMP→%s: success", v.Name, target)
			return true
		}

		v.t.Logf("[%s] probe ICMP→%s: %s (retrying)", v.Name, target, resp.Error)
		time.Sleep(1 * time.Second)
	}

	v.t.Errorf("[%s] probe ICMP→%s: timed out after %v", v.Name, target, timeout)
	return false
}

// ProbeTCP sends a TCP probe request via the VM's probe server, retrying
// until success or timeout.
func (v *KubespandVM) ProbeTCP(target string, port int, timeout time.Duration) bool {
	v.t.Helper()
	deadline := time.Now().Add(timeout)

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		conn, err := grpc.NewClient(v.probeAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			v.t.Logf("[%s] probe TCP connect: %v", v.Name, err)
			time.Sleep(1 * time.Second)
			continue
		}

		client := pb.NewProbeServiceClient(conn)
		resp, err := client.TCPProbe(ctx, &pb.TCPProbeRequest{
			Target:         target,
			Port:           int32(port),
			TimeoutSeconds: 10,
		})
		cancel()
		conn.Close()

		if err != nil {
			v.t.Logf("[%s] probe TCP→%s:%d: %v", v.Name, target, port, err)
			time.Sleep(1 * time.Second)
			continue
		}

		if resp.Success {
			v.t.Logf("[%s] probe TCP→%s:%d: success", v.Name, target, port)
			return true
		}

		v.t.Logf("[%s] probe TCP→%s:%d: %s (retrying)", v.Name, target, port, resp.Error)
		time.Sleep(1 * time.Second)
	}

	v.t.Errorf("[%s] probe TCP→%s:%d: timed out after %v", v.Name, target, port, timeout)
	return false
}

// DumpDiagnostics fetches routing/WG/nftables state from the VM's probe server.
func (v *KubespandVM) DumpDiagnostics() string {
	v.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, err := grpc.NewClient(v.probeAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		v.t.Logf("[%s] diagnostics connect: %v", v.Name, err)
		return ""
	}
	defer conn.Close()

	client := pb.NewProbeServiceClient(conn)
	resp, err := client.Diagnostics(ctx, &pb.DiagnosticsRequest{})
	if err != nil {
		v.t.Logf("[%s] diagnostics rpc: %v", v.Name, err)
		return ""
	}
	return resp.Output
}

// PollPeerStatus connects to kubespand's COSI API and polls PeerStatus
// resources until at least minPeers report state "up".
// Requires withCOSI=true in BootKubespandVM.
func (v *KubespandVM) PollPeerStatus(minPeers int, timeout time.Duration) ([]KubespanPeerResult, error) {
	v.t.Helper()

	if v.cosiAddr == "" {
		v.t.Fatalf("[%s] PollPeerStatus called but COSI not configured", v.Name)
	}

	deadline := time.Now().Add(timeout)
	var lastErr string

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		conn, err := grpc.NewClient(v.cosiAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			lastErr = err.Error()
			v.t.Logf("[%s] COSI connect (waiting): %s", v.Name, lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		st := state.WrapCore(stateclient.NewAdapter(v1alpha1.NewStateClient(conn)))
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, st)
		cancel()
		conn.Close()

		if err != nil {
			lastErr = err.Error()
			v.t.Logf("[%s] COSI poll (waiting): %s", v.Name, lastErr)
			time.Sleep(1 * time.Second)
			continue
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

		upCount := 0
		for _, p := range peers {
			if p.State == kubespan.PeerStateUp {
				upCount++
			}
		}

		var peerSummary strings.Builder
		for i, p := range peers {
			if i > 0 {
				peerSummary.WriteString("; ")
			}
			fmt.Fprintf(&peerSummary, "%s state=%s ep=%s", p.Label, p.State, p.Endpoint)
		}
		v.t.Logf("[%s] COSI poll: %d peers, %d up (need %d) [%s]", v.Name, len(peers), upCount, minPeers, peerSummary.String())

		if upCount >= minPeers {
			return peers, nil
		}

		time.Sleep(1 * time.Second)
	}

	return nil, fmt.Errorf("timeout after %v waiting for %d peers up, last error: %s", timeout, minPeers, lastErr)
}
