package executor

import (
	"context"
	"net"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedbackend"
	execfake "github.com/agentydragon/ducktape/agentplane/testing/executor"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestSilentSetupCompletesAfterLongReadTimeout(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	pb.RegisterExecutorServer(server, typedbackend.New(core))
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })
	conn, err := grpc.NewClient("passthrough:///native", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client := pb.NewExecutorClient(conn)
	fake := &execfake.Client{
		OnOpenSession: func(ctx context.Context, r *pb.OpenSessionRequest) (grpc.ServerStreamingClient[pb.OpenSessionResponse], error) {
			return client.OpenSession(ctx, r)
		},
		OnStartProcess: func(ctx context.Context, r *pb.StartProcessRequest) (*pb.StartProcessResponse, error) {
			return client.StartProcess(ctx, r)
		},
		OnReadProcess: func(ctx context.Context, r *pb.ReadProcessRequest) (*pb.ReadProcessResponse, error) {
			return client.ReadProcess(ctx, r)
		},
	}
	store, owner := claimedExecutorStore(t)
	e := newFakeExecutor(t, store, owner, fake)
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	defer cancel()
	var output string
	code, err := e.Setup(ctx, "sleep 2; printf 'awake\\n'", func(_ string, data []byte) error { output += string(data); return nil })
	if err != nil || code != 0 || output != "awake\n" {
		t.Fatalf("setup code=%d output=%q error=%v", code, output, err)
	}
}
