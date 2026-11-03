// runtime_probe observes a disposable guest over the real Gateway mTLS protocol.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

func run() error {
	address := flag.String("address", "192.0.2.2:9090", "guest Runtime endpoint")
	tlsDir := flag.String("tls-dir", "", "directory containing Gateway tls.crt, tls.key and ca.crt")
	namespace := flag.String("namespace", "firecracker-environment-proof", "Sandbox namespace")
	sandbox := flag.String("sandbox-uid", "proof-sandbox", "Sandbox UID")
	pod := flag.String("pod-uid", "proof-pod", "unchanged launcher Pod UID")
	credential := flag.String("credential-id", "proof-credential", "Runtime credential ID")
	epoch := flag.Uint64("epoch", 1, "strictly increasing connection epoch")
	expectedBoot := flag.String("expected-boot-id", "", "require this Runtime process identity after resume")
	flag.Parse()
	if *tlsDir == "" || flag.NArg() != 0 {
		return fmt.Errorf("--tls-dir is required; positional arguments are forbidden")
	}
	uri, err := transport.EnvironmentRuntimeURI(*namespace, *sandbox, *credential)
	if err != nil {
		return err
	}
	security := transport.Security{Mode: transport.MutualTLS, TLS: &transport.Config{
		CertFile: filepath.Join(*tlsDir, "tls.crt"), KeyFile: filepath.Join(*tlsDir, "tls.key"), CAFile: filepath.Join(*tlsDir, "ca.crt"),
	}}
	credentials, err := security.ClientCredentials(uri)
	if err != nil {
		return err
	}
	connection, err := grpc.NewClient(*address, grpc.WithTransportCredentials(credentials))
	if err != nil {
		return err
	}
	defer connection.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	client := pb.NewEnvironmentRuntimeClient(connection)
	watch, err := client.WatchState(ctx, &pb.WatchStateRequest{SandboxUid: *sandbox, IncarnationUid: *pod, ConnectionEpoch: *epoch})
	if err != nil {
		return err
	}
	first, err := watch.Recv()
	if err != nil {
		return err
	}
	boot := first.GetEnvironmentRuntimeBootId()
	if boot == "" || (*expectedBoot != "" && boot != *expectedBoot) {
		return fmt.Errorf("Runtime boot ID %q differs from expected %q", boot, *expectedBoot)
	}
	if first.GetIncarnationUid() != *pod || first.GetConnectionEpoch() != *epoch {
		return fmt.Errorf("Runtime returned a different incarnation or connection epoch")
	}
	call := metadata.AppendToOutgoingContext(ctx, control.SandboxMetadata, *sandbox, control.IncarnationMetadata, *pod, control.BootMetadata, boot, control.EpochMetadata, strconv.FormatUint(*epoch, 10))
	status, err := client.GetStatus(call, &sessionpb.GetStatusRequest{})
	if err != nil {
		return err
	}
	if status.GetEnvironmentRuntimeBootId() != boot {
		return fmt.Errorf("GetStatus changed Runtime boot ID")
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"runtime_boot_id": boot, "pod_uid": *pod, "connection_epoch": *epoch, "status": "passed"})
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
