// Binary trustd is the PID-1 init for the trustd CSR flow test VM.
// Starts kubespand (with ca_crt + token + apid_path for trustd CSR flow)
// and monitors its progress via COSI state, emitting diagnostic events.
//
// kubespand manages the apid subprocess: it waits for secrets.API (using
// Talos's APIReadyCondition), then starts apid which serves mTLS on :50000.
package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"net"
	"os/exec"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/kubespanlib"
)

func main() {
	initlib.InitBasic()
	if err := run(); err != nil {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: err.Error()})
		initlib.Poweroff()
	}
	// Idle forever — test observes via Talos API from outside.
	select {}
}

func run() error {
	params := initlib.ParseCmdline()

	clusterID := params["cluster_id"]
	sharedSecret := params["shared_secret"]
	discoveryAddr := params["discovery"]
	caCrtB64 := params["ca_crt"]
	token := params["token"]
	clusterEndpoint := params["cluster_endpoint"]

	if clusterID == "" || sharedSecret == "" || discoveryAddr == "" || caCrtB64 == "" || token == "" || clusterEndpoint == "" {
		return fmt.Errorf("missing required kernel cmdline params: cluster_id=%s discovery=%s ca_crt_len=%d token=%s endpoint=%s",
			clusterID, discoveryAddr, len(caCrtB64), token, clusterEndpoint)
	}

	caCrtPEM, err := base64.StdEncoding.DecodeString(caCrtB64)
	if err != nil {
		return fmt.Errorf("ca_crt base64 decode failed: %w", err)
	}

	kubespanlib.LoadModules()

	// eth0: L2 segment (mcast NIC) for KubeSpan mesh.
	kubespanlib.ConfigureNetwork("192.168.50.1", "24")

	// eth1: mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.WaitForInterface("eth1")
	initlib.MustRun("ip", "link", "set", "eth1", "up")
	initlib.MustRun("ip", "addr", "add", "10.0.2.15/24", "dev", "eth1")

	cfg := kubespanlib.KubespandConfig{
		ClusterID:       clusterID,
		SharedSecret:    sharedSecret,
		DiscoveryAddr:   discoveryAddr,
		ListenPort:      51820,
		EndpointFilters: []string{"192.168.50.0/24"},
		ClusterEndpoint: clusterEndpoint,
		CACrt:           string(caCrtPEM),
		Token:           token,
		ApidPath:        "/apid",
		// Include 127.0.0.1 in cert SANs for port-forwarded test connections.
		CertSANs: []string{"127.0.0.1"},
	}
	kubespandCmd := kubespanlib.StartKubespand(cfg)

	// Monitor kubespand process and COSI state in the background.
	go monitorLoop(kubespandCmd)

	// Probe apid's TLS port to detect when it starts serving mTLS.
	// kubespand waits for secrets.API then starts apid, which listens on :50000.
	go func() {
		for {
			time.Sleep(5 * time.Second)
			conn, err := net.DialTimeout("tcp", "127.0.0.1:50000", 2*time.Second)
			if err != nil {
				initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: fmt.Sprintf("apid TLS port probe: %v", err)})
				continue
			}
			conn.Close()
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "apid TLS port 50000 is listening!"})
			return
		}
	}()

	return nil
}

// monitorLoop periodically checks process health, queries COSI state for the
// trustd CSR flow resources, and dumps diagnostic logs. Emits events for each
// significant state change.
func monitorLoop(kubespandCmd *exec.Cmd) {
	var (
		cosiState    state.State
		lastOSRoot   bool
		lastCertSAN  bool
		lastAPI      bool
		lastPeerSeen bool
	)

	for i := 0; ; i++ {
		time.Sleep(5 * time.Second)

		// Check process health.
		if kubespandCmd.ProcessState != nil {
			initlib.EmitEvent(qemu_tests.Event{
				Type:    qemu_tests.EventError,
				Message: fmt.Sprintf("kubespand exited: %s", kubespandCmd.ProcessState),
			})
			initlib.DumpLog("/tmp/kubespand.log")
			break
		}

		// Connect to COSI socket (lazily).
		if cosiState == nil {
			var err error
			cosiState, _, err = kubespanlib.NewCOSIClient("/system/run/machined/machine.sock")
			if err != nil {
				if i%6 == 0 { // every 30s
					initlib.EmitEvent(qemu_tests.Event{
						Type:    qemu_tests.EventKubespand,
						Message: fmt.Sprintf("waiting for COSI socket: %v", err),
					})
				}
				continue
			}
		}

		// Probe each resource in the CSR flow chain.
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)

		hasOSRoot := resourceExists(ctx, cosiState, secrets.NamespaceName, secrets.OSRootType, secrets.OSRootID)
		hasCertSAN := resourceExists(ctx, cosiState, secrets.NamespaceName, secrets.CertSANType, secrets.CertSANAPIID)
		hasAPI := resourceExists(ctx, cosiState, secrets.NamespaceName, secrets.APIType, secrets.APIID)

		// Check peer discovery.
		var peerSummary string
		hasPeer := false
		if list, err := safe.StateListAll[*kubespan.PeerSpec](ctx, cosiState); err == nil {
			for it := list.Iterator(); it.Next(); {
				ps := it.Value()
				hasPeer = true
				peerSummary += fmt.Sprintf(" [%s addr=%s endpoints=%v]",
					ps.TypedSpec().Label, ps.TypedSpec().Address, ps.TypedSpec().Endpoints)
			}
		}

		cancel()

		// Emit events on state changes.
		if hasOSRoot && !lastOSRoot {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "secrets.OSRoot created"})
		}
		if hasCertSAN && !lastCertSAN {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "secrets.CertSAN created"})
		}
		if hasAPI && !lastAPI {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "secrets.API created (trustd CSR flow complete!)"})
		}
		if hasPeer && !lastPeerSeen {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDiscovery, Message: "first peer discovered:" + peerSummary})
		}

		lastOSRoot = hasOSRoot
		lastCertSAN = hasCertSAN
		lastAPI = hasAPI
		lastPeerSeen = hasPeer

		// Periodic status dump every 30s.
		if i%6 == 0 {
			initlib.EmitEvent(qemu_tests.Event{
				Type: qemu_tests.EventKubespand,
				Message: fmt.Sprintf("CSR flow status: OSRoot=%v CertSAN=%v API=%v peers=%v%s",
					hasOSRoot, hasCertSAN, hasAPI, hasPeer, peerSummary),
			})
		}

		// At 60s, 120s, 180s: dump detailed diagnostics.
		elapsed := time.Duration(i+1) * 5 * time.Second
		if elapsed == 60*time.Second || elapsed == 120*time.Second || elapsed == 180*time.Second {
			initlib.EmitEvent(qemu_tests.Event{
				Type:    qemu_tests.EventKubespand,
				Message: fmt.Sprintf("=== DIAGNOSTICS DUMP at %s ===", elapsed),
			})
			kubespanlib.DumpDiagnostics()
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventKubespand, Message: "--- kubespand.log tail ---"})
			initlib.DumpLog("/tmp/kubespand.log")
		}
	}
}

// resourceExists checks if a single COSI resource exists by type and ID.
func resourceExists(ctx context.Context, st state.State, ns resource.Namespace, typ resource.Type, id resource.ID) bool {
	md := resource.NewMetadata(ns, typ, id, resource.VersionUndefined)
	_, err := st.Get(ctx, md)
	return err == nil
}
