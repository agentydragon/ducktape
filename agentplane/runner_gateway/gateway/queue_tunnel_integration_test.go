package gateway

import (
	"context"
	"database/sql"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

type queueCatalog struct{ sandbox dispatcher.SandboxSnapshot }

func (c queueCatalog) GetSandbox(_ context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c queueCatalog) ListSandboxes(context.Context) ([]dispatcher.SandboxSnapshot, error) {
	return []dispatcher.SandboxSnapshot{c.sandbox}, nil
}
func (queueCatalog) Changes() <-chan struct{}        { return nil }
func (queueCatalog) WaitSynced(context.Context) bool { return true }

type queueWake struct{}

func (queueWake) WakeForSend(context.Context, string, string, func(int64) error) (bool, error) {
	return false, errors.New("unexpected wake for ready Sandbox")
}

type queueLease struct{ store *dispatcher.Store }

func (l queueLease) Acquire(ctx context.Context, _, uid string) (*dispatcher.Owner, error) {
	generation, err := l.store.OwnershipGeneration(ctx, uid)
	if err != nil {
		return nil, err
	}
	return l.store.ClaimSandboxOwner(ctx, uid, "queue-test-lease", 1, "queue-test-replica", generation)
}

func (l queueLease) StillOwns(ctx context.Context, owner dispatcher.Owner) (bool, error) {
	return l.store.IsCurrentOwner(ctx, owner)
}

// This test starts with a committed queue row and ends with the runner's
// admission returned over the real Gateway tunnel, without bypassing the
// Dispatcher worker.
func TestQueuedDeliveryThroughGatewayTunnel(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	store := dispatcher.NewStore(db, 4)
	command := &commandpb.Command{CommandId: "queue-tunnel-command", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "persisted input"},
	}}
	f := newSandboxFixture(t)
	sandboxUID := string(f.sandbox.UID)
	queued, err := store.Enqueue(ctx, "session-queue", testSandbox, sandboxUID, command, nil)
	if err != nil || queued.State != dispatcher.QueueState {
		t.Fatalf("queue receipt: %+v %v", queued, err)
	}

	gateway := f.newGateway(t, "test-runner-gateway-0", "192.0.2.10")
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	gateway.RegisterProxy(server)

	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	defer listener.Close()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	runnerListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	runnerServer := grpc.NewServer(grpc.Creds(f.serverCredentials(t)))
	gateway.RegisterRunnerControl(runnerServer)
	go func() { _ = runnerServer.Serve(runnerListener) }()
	defer runnerServer.Stop()
	runnerConnection, err := grpc.NewClient(runnerListener.Addr().String(), grpc.WithTransportCredentials(f.clientCredentials(t)))
	if err != nil {
		t.Fatal(err)
	}
	defer runnerConnection.Close()

	streamCtx := metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+f.runnerToken))
	stream, err := runnercontrolpb.NewRunnerControlClient(runnerConnection).Connect(streamCtx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Hello{
		Hello: &runnercontrolpb.Hello{RunnerBootId: "queue-boot"},
	}}); err != nil {
		t.Fatal(err)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetWelcome() == nil {
		t.Fatalf("runner welcome: %v %v", frame, err)
	}

	tokenFile := filepath.Join(t.TempDir(), "caller-token")
	if err := os.WriteFile(tokenFile, []byte(f.appToken), 0o600); err != nil {
		t.Fatal(err)
	}
	hub := dispatcher.NewWatchHub(integrationDatabase.URL)
	hubDone := make(chan error, 1)
	go func() { hubDone <- hub.Run(ctx) }()
	for !hub.Connected() {
		if err := ctx.Err(); err != nil {
			t.Fatalf("PostgreSQL listener unavailable: %v", err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	worker := &dispatcher.DeliveryWorker{
		Store: store, Catalog: queueCatalog{dispatcher.SandboxSnapshot{
			Name: testSandbox, Namespace: f.namespace, UID: sandboxUID, Incarnation: string(f.pod.UID),
		}}, Wake: queueWake{}, Leases: queueLease{store: store},
		Runner: &dispatcher.ProxyRunner{
			Namespace: f.namespace, TokenFile: tokenFile, Timeout: 5 * time.Second,
			TargetTemplate: listener.Addr().String(),
		},
		Hub: hub, Ready: make(chan struct{}),
	}
	workerDone := make(chan error, 1)
	go func() { workerDone <- worker.Run(ctx) }()
	select {
	case <-worker.Ready:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}

	input := receiveTunnelCall(t, stream)
	var request runnerservicepb.SubmitInputRequest
	if err := proto.Unmarshal(input.GetRequest(), &request); err != nil {
		t.Fatal(err)
	}
	if input.GetMethod() != "/agentplane.runner.Runner/SubmitInput" ||
		request.GetCommandId() != command.GetCommandId() || request.GetSessionId() != "session-queue" ||
		request.GetText() != "persisted input" {
		t.Fatalf("queued request changed before runner admission: call=%v request=%v", input, &request)
	}
	admission, err := proto.Marshal(&runnerservicepb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 81}})
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: input.GetCallId(), Response: admission},
	}}); err != nil {
		t.Fatal(err)
	}
	for {
		row, err := store.GetDispatch(ctx, command.GetCommandId())
		if err != nil {
			t.Fatal(err)
		}
		if row.State == dispatcher.AdmittedState {
			if row.Admission.GetCursor() != 81 {
				t.Fatalf("wrong stored runner admission: %+v", row)
			}
			break
		}
		if err := ctx.Err(); err != nil {
			t.Fatalf("queued delivery never admitted: %+v %v", row, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-workerDone:
	case <-time.After(time.Second):
		t.Fatal("worker did not stop")
	}
	select {
	case <-hubDone:
	case <-time.After(time.Second):
		t.Fatal("PostgreSQL listener did not stop")
	}
}
