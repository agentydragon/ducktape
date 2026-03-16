// Binary trustd is the PID-1 init for the trustd CSR flow test VM.
// Starts kubespand (with ca_crt + token + apid_path for trustd CSR flow)
// and monitors its progress via COSI state, emitting diagnostic logs.
//
// kubespand manages the apid subprocess: it waits for secrets.API (using
// Talos's APIReadyCondition), then starts apid which serves mTLS on :50000.
//
// Kubespand agent config is provided via a CIDATA virtio drive.
package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"os/exec"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/kubespanlib"
)

func main() {
	initlib.Init()
	if err := run(); err != nil {
		log.Fatalf("trustd init: %v", err)
	}
	// Idle until the test host kills the VM.
	select {}
}

func run() error {
	kubespanlib.LoadModules()

	// eth0: L2 segment (mcast NIC) for KubeSpan mesh.
	kubespanlib.ConfigureNetwork("192.168.50.1", "24")

	// mgmt NIC (QEMU user-mode) for port forwarding to the test host.
	initlib.ConfigureMgmtNIC(true)

	// Load kubespand config from CIDATA drive and start.
	initlib.MountKubespandCIDATA()
	kubespandCmd := kubespanlib.StartKubespand()

	// Monitor kubespand process and COSI state in the background.
	go monitorLoop(kubespandCmd)

	// Probe apid's TLS port to detect when it starts serving mTLS.
	// kubespand waits for secrets.API then starts apid, which listens on :50000.
	go func() {
		for {
			time.Sleep(5 * time.Second)
			conn, err := net.DialTimeout("tcp", "127.0.0.1:50000", 2*time.Second)
			if err != nil {
				log.Printf("apid TLS port probe: %v", err)
				continue
			}
			conn.Close()
			log.Printf("apid TLS port 50000 is listening!")
			return
		}
	}()

	return nil
}

// newCOSIClient connects to a local COSI socket and returns a state.State.
func newCOSIClient(socketPath string) (state.State, *grpc.ClientConn, error) {
	conn, err := grpc.NewClient(
		"unix://"+socketPath,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, nil, err
	}
	return state.WrapCore(stateclient.NewAdapter(v1alpha1.NewStateClient(conn))), conn, nil
}

// monitorLoop periodically checks process health, queries COSI state for the
// trustd CSR flow resources, and dumps diagnostic logs.
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
			log.Printf("kubespand exited: %s", kubespandCmd.ProcessState)
			initlib.DumpLog("/tmp/kubespand.log")
			break
		}

		// Connect to COSI socket (lazily).
		if cosiState == nil {
			var err error
			cosiState, _, err = newCOSIClient("/system/run/machined/machine.sock")
			if err != nil {
				if i%6 == 0 { // every 30s
					log.Printf("waiting for COSI socket: %v", err)
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

		// Log state changes.
		if hasOSRoot && !lastOSRoot {
			log.Printf("secrets.OSRoot created")
		}
		if hasCertSAN && !lastCertSAN {
			log.Printf("secrets.CertSAN created")
		}
		if hasAPI && !lastAPI {
			log.Printf("secrets.API created (trustd CSR flow complete!)")
		}
		if hasPeer && !lastPeerSeen {
			log.Printf("first peer discovered:%s", peerSummary)
		}

		lastOSRoot = hasOSRoot
		lastCertSAN = hasCertSAN
		lastAPI = hasAPI
		lastPeerSeen = hasPeer

		// Periodic status dump every 30s.
		if i%6 == 0 {
			log.Printf("CSR flow status: OSRoot=%v CertSAN=%v API=%v peers=%v%s",
				hasOSRoot, hasCertSAN, hasAPI, hasPeer, peerSummary)
		}

		// At 60s, 120s, 180s: dump kubespand log.
		elapsed := time.Duration(i+1) * 5 * time.Second
		if elapsed == 60*time.Second || elapsed == 120*time.Second || elapsed == 180*time.Second {
			log.Printf("=== DIAGNOSTICS DUMP at %s ===", elapsed)
			log.Printf("--- kubespand.log tail ---")
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
