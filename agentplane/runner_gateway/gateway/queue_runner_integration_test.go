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
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
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

type queuedRunnerRequest struct {
	request  *runnerservicepb.SubmitInputRequest
	metadata metadata.MD
}

type queueRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	bootID    string
	submitted chan queuedRunnerRequest
}

func (r *queueRunner) WatchState(request *runnerservicepb.WatchStateRequest, stream grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse]) error {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if err := stream.Send(&runnerservicepb.WatchStateResponse{
			SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
			RunnerBootId: r.bootID, ConnectionEpoch: request.ConnectionEpoch,
			Facts: &runnerstatepb.RunnerFacts{},
		}); err != nil {
			return err
		}
		select {
		case <-stream.Context().Done():
			return stream.Context().Err()
		case <-ticker.C:
		}
	}
}

func (r *queueRunner) SubmitInput(ctx context.Context, request *runnerservicepb.SubmitInputRequest) (*runnerservicepb.SubmitInputResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	r.submitted <- queuedRunnerRequest{request: request, metadata: md}
	return &runnerservicepb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 81}}, nil
}

// This test starts with a committed queue row and ends with the Runner's durable admission returned
// through a direct mTLS Gateway-to-Runner RPC, without bypassing the Dispatcher worker.
func TestQueuedDeliveryThroughGatewayRunnerRPC(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	store := dispatcher.NewStore(db, 4)
	command := &commandpb.Command{CommandId: "queue-runner-command", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "persisted input"},
	}}
	f := newSandboxFixture(t)
	sandboxUID := string(f.sandbox.UID)
	queued, err := store.Enqueue(ctx, "session-queue", testSandbox, sandboxUID, command, nil)
	if err != nil || queued.State != dispatcher.QueueState {
		t.Fatalf("queue receipt: %+v %v", queued, err)
	}

	gateway := f.newGateway(t, "test-runner-gateway", "192.0.2.10")
	runnerDestination, err := gateway.runnerDestination(ctx, f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	runnerListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	runnerCredentials, err := f.runnerTLS.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	runnerServer := grpc.NewServer(grpc.Creds(runnerCredentials))
	runner := &queueRunner{bootID: "queue-runner-boot", submitted: make(chan queuedRunnerRequest, 1)}
	runnerservicepb.RegisterRunnerServer(runnerServer, runner)
	go func() { _ = runnerServer.Serve(runnerListener) }()
	defer runnerServer.Stop()
	runnerDestination.target = runnerListener.Addr().String()

	observerCtx, stopObserver := context.WithCancel(ctx)
	observerDone := make(chan error, 1)
	go func() { observerDone <- gateway.observeRunner(observerCtx, runnerDestination) }()
	select {
	case err := <-observerDone:
		t.Fatalf("Runner observer stopped: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("Runner observer did not become callable")
	case <-waitForRunnerConnection(ctx, gateway, sandboxUID):
	}
	defer func() {
		stopObserver()
		select {
		case <-observerDone:
		case <-time.After(time.Second):
			t.Error("Runner observer did not stop")
		}
	}()

	proxyListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxyServer := grpc.NewServer(gateway.Methods().ServerOptions(0)...)
	gateway.RegisterProxy(proxyServer)
	go func() { _ = proxyServer.Serve(proxyListener) }()
	defer proxyServer.Stop()
	defer proxyListener.Close()

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
			TargetTemplate: proxyListener.Addr().String(),
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

	select {
	case received := <-runner.submitted:
		request := received.request
		if request.GetCommandId() != command.GetCommandId() || request.GetSessionId() != "session-queue" || request.GetText() != "persisted input" {
			t.Fatalf("queued request changed before Runner admission: %v", request)
		}
		if got := received.metadata.Get("agentplane-sandbox-uid"); len(got) != 1 || got[0] != sandboxUID {
			t.Fatalf("Gateway omitted the Sandbox UID fence: %v", received.metadata)
		}
		if got := received.metadata.Get("agentplane-incarnation-uid"); len(got) != 1 || got[0] != string(f.pod.UID) {
			t.Fatalf("Gateway omitted the incarnation fence: %v", received.metadata)
		}
		if got := received.metadata.Get("agentplane-runner-boot-id"); len(got) != 1 || got[0] != runner.bootID {
			t.Fatalf("Gateway omitted the Runner boot fence: %v", received.metadata)
		}
		if got := received.metadata.Get("agentplane-connection-epoch"); len(got) != 1 || got[0] == "" {
			t.Fatalf("Gateway omitted the Lease epoch fence: %v", received.metadata)
		}
	case <-ctx.Done():
		t.Fatal("queued command did not reach Runner")
	}
	for {
		row, err := store.GetDispatch(ctx, command.GetCommandId())
		if err != nil {
			t.Fatal(err)
		}
		if row.State == dispatcher.AdmittedState {
			if row.Admission.GetCursor() != 81 {
				t.Fatalf("wrong stored Runner admission: %+v", row)
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

func waitForRunnerConnection(ctx context.Context, gateway *Gateway, sandboxUID string) <-chan struct{} {
	ready := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			if _, err := gateway.connections.holder(sandboxUID); err == nil {
				close(ready)
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	return ready
}

var _ runnerservicepb.RunnerServer = (*queueRunner)(nil)
