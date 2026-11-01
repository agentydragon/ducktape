package harnesshost

import (
	"bytes"
	"context"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/retained"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type privateRuntimePeer struct {
	runtimepb.UnimplementedEnvironmentRuntimeServer
	hello      chan *runtimepb.HostHello
	intents    chan *runtimepb.LifecycleIntent
	reports    chan *runtimepb.HostReport
	disconnect chan struct{}
}

func (p *privateRuntimePeer) WaitBootstrap(context.Context, *runtimepb.Binding) (*emptypb.Empty, error) {
	return &emptypb.Empty{}, nil
}

func (p *privateRuntimePeer) Connect(stream runtimepb.EnvironmentRuntime_ConnectServer) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	select {
	case p.hello <- first.GetHello():
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	errorsCh := make(chan error, 1)
	go func() {
		for {
			message, err := stream.Recv()
			if err != nil {
				errorsCh <- err
				return
			}
			select {
			case p.reports <- message.GetReport():
			case <-stream.Context().Done():
				return
			}
		}
	}()
	for {
		select {
		case intent := <-p.intents:
			if err := stream.Send(intent); err != nil {
				return err
			}
		case err := <-errorsCh:
			return err
		case <-p.disconnect:
			return status.Error(codes.Unavailable, "Runtime process lost")
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
}

// Re-executed by TestMain. This is a separate OS process using the production
// Host Run path, with a private lifecycle server owned by the parent test.
func hostBoundaryChild(root string) int {
	r, err := New(Config{StateDir: root, RuntimeSocket: filepath.Join(root, "runtime.sock"), ListenSocket: filepath.Join(root, "host.sock"), Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}})
	if err != nil {
		return 2
	}
	if err := r.Run(context.Background(), configuration.Endpoint{Target: "127.0.0.1:1"}); err != nil {
		return 3
	}
	return 0
}

func TestHostProcessBindingAndRuntimeLoss(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	listener, err := net.Listen("unix", filepath.Join(root, "runtime.sock"))
	if err != nil {
		t.Fatal(err)
	}
	peer := &privateRuntimePeer{hello: make(chan *runtimepb.HostHello, 1), intents: make(chan *runtimepb.LifecycleIntent), reports: make(chan *runtimepb.HostReport, 16), disconnect: make(chan struct{})}
	server := grpc.NewServer()
	runtimepb.RegisterEnvironmentRuntimeServer(server, peer)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	child := exec.CommandContext(ctx, binary, "--host-boundary-child", root)
	var output bytes.Buffer
	child.Stdout = &output
	child.Stderr = &output
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- child.Wait() }()
	var hello *runtimepb.HostHello
	select {
	case hello = <-peer.hello:
	case err := <-done:
		t.Fatalf("Host exited before hello: %v: %s", err, output.String())
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	if hello.HostBootId == "" || hello.ControlSocket != filepath.Join(root, "host.sock") {
		t.Fatalf("invalid hello %+v", hello)
	}
	intent := &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime-process", HostBootId: hello.HostBootId, Generation: 1}, SandboxUid: "sandbox", IncarnationUid: "incarnation", Mode: runtimepb.LifecycleIntent_RUNNING}
	select {
	case peer.intents <- intent:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	for {
		select {
		case report := <-peer.reports:
			if report.Ready && report.Binding.Generation == 1 {
				goto ready
			}
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
ready:
	conn, err := grpc.NewClient("unix://"+hello.ControlSocket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	valid := metadata.NewOutgoingContext(ctx, metadata.Pairs(runtimeBootMetadata, intent.Binding.RuntimeBootId, hostBootMetadata, hello.HostBootId, generationMetadata, "1"))
	if _, err := client.GetStatus(valid, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("bound RPC: %v", err)
	}
	stale := metadata.NewOutgoingContext(ctx, metadata.Pairs(runtimeBootMetadata, intent.Binding.RuntimeBootId, hostBootMetadata, hello.HostBootId, generationMetadata, "2"))
	if _, err := client.GetStatus(stale, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale RPC: %v", err)
	}
	owner, err := retained.OpenOwner(filepath.Join(root, "harness-host"))
	if err == nil {
		_ = owner.Close()
		t.Fatal("live Host did not retain owner")
	}
	close(peer.disconnect)
	select {
	case err := <-done:
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 3 {
			t.Fatalf("control loss exit: %v, %s", err, output.String())
		}
	case <-ctx.Done():
		t.Fatal("Host did not exit after Runtime loss")
	}
	owner, err = retained.OpenOwner(filepath.Join(root, "harness-host"))
	if err != nil {
		t.Fatalf("Host retained owner after cleanup: %v", err)
	}
	_ = owner.Close()
	if _, err := os.Stat(filepath.Join(root, "sessions", "sealed")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("control loss manufactured environment seal: %v", err)
	}
}
