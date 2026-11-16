package runtimeproxy

import (
	"context"
	"database/sql"
	"net"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type queueCatalog struct{ sandbox dispatcher.SandboxSnapshot }

const queueIdentityUID = "be36780a-b018-4d42-96a3-e8cc68bbe76a"

func (queueCatalog) GetExecutionIdentity(context.Context, dispatcher.SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return &identitypb.Identity{Name: "queue-identity", Uid: queueIdentityUID}, true, nil
}

func (c queueCatalog) GetSandbox(_ context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c queueCatalog) ListSandboxes(context.Context) ([]dispatcher.SandboxSnapshot, error) {
	return []dispatcher.SandboxSnapshot{c.sandbox}, nil
}

func (c queueCatalog) CurrentSandboxUIDs(context.Context) (map[string]struct{}, error) {
	return map[string]struct{}{c.sandbox.UID: {}}, nil
}

func (queueCatalog) WatchSandboxes(func(dispatcher.SandboxKey)) (func(), error) {
	return func() {}, nil
}

func (c queueCatalog) SandboxExists(_ context.Context, key dispatcher.SandboxKey) (bool, error) {
	return c.sandbox.UID == key.UID, nil
}
func (queueCatalog) WaitSynced(context.Context) bool { return true }

type queueWake struct{}

func (queueWake) RequestWake(context.Context, *lifecycle.Target, lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	return &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_NOT_NEEDED}, nil
}

type queuedRuntimeRequest struct {
	request  *environmentruntimepb.SubmitInputRequest
	metadata metadata.MD
}

type queueRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	bootID    string
	submitted chan queuedRuntimeRequest
}

func (r *queueRuntime) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	if request.GetObserveOnly() {
		return status.Error(codes.FailedPrecondition, "Server request must use an independent observation")
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if err := stream.Send(&environmentruntimepb.WatchStateResponse{
			SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
			EnvironmentRuntimeBootId: r.bootID,
			Facts:                    &environmentruntimestatepb.EnvironmentRuntimeFacts{},
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

func (r *queueRuntime) SubmitInput(ctx context.Context, request *environmentruntimepb.SubmitInputRequest) (*environmentruntimepb.SubmitInputResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	r.submitted <- queuedRuntimeRequest{request: request, metadata: md}
	return &environmentruntimepb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 81}}, nil
}

// This test starts with a committed queue row and ends with the Runtime's durable admission returned
// through a direct mTLS Server-to-Runtime RPC, without bypassing the Dispatcher worker.
func TestQueuedDeliveryThroughRuntimeProxy(t *testing.T) {
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
	spec, err := proto.Marshal(&sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: testSandbox, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}})
	if err != nil {
		t.Fatal(err)
	}
	actor := &identitypb.ServiceAccountIdentity{
		Account: &identitypb.ServiceAccount{Namespace: "gateway-test", Name: "queue-writer"},
		Uid:     "gateway-test-actor-uid",
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO session(session_id,sandbox_name,sandbox_uid,spec,state,
		execution_identity_name,execution_identity_uid,
		creator_account_namespace,creator_account_name,creator_account_uid,
		effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
		created_at,updated_at)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$8,$9,$10,now(),now())`, "session-queue", testSandbox, sandboxUID, spec,
		dispatcher.SessionAcceptedState, "queue-identity", queueIdentityUID,
		actor.GetAccount().GetNamespace(), actor.GetAccount().GetName(), actor.GetUid()); err != nil {
		t.Fatal(err)
	}
	attribution := &dispatcher.AdmissionAttribution{Actor: actor, EffectiveCaller: actor}
	caller := dispatcher.ServiceAccountCaller{ServiceAccountIdentity: actor, Privileged: true, Attribution: attribution}
	queued, err := store.EnqueueForCaller(ctx, "session-queue", testSandbox, sandboxUID, command, nil, caller)
	if err != nil || queued.State != dispatcher.QueueState {
		t.Fatalf("queue receipt: %+v %v", queued, err)
	}

	server := f.newServer(t)
	runtimeDestination, err := server.runtimeDestination(ctx, f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	runtimeListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	runtimeCredentials, err := f.runtimeTLS.ServerCredentials(peertls.RuntimeProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	runtimeServer := grpc.NewServer(grpc.Creds(runtimeCredentials))
	runtime := &queueRuntime{bootID: "queue-runner-boot", submitted: make(chan queuedRuntimeRequest, 1)}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(runtimeServer, runtime)
	go func() { _ = runtimeServer.Serve(runtimeListener) }()
	defer runtimeServer.Stop()
	runtimeDestination.target = runtimeListener.Addr().String()

	observerCtx, stopObserver := context.WithCancel(ctx)
	observerDone := make(chan error, 1)
	go func() { observerDone <- server.observeRuntime(observerCtx, runtimeDestination) }()
	select {
	case err := <-observerDone:
		t.Fatalf("Runtime observer stopped: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("Runtime observer did not become callable")
	case <-waitForRuntimeConnection(ctx, server, sandboxUID):
	}
	defer func() {
		stopObserver()
		select {
		case <-observerDone:
		case <-time.After(time.Second):
			t.Error("Runtime observer did not stop")
		}
	}()

	proxy := serveServer(t, server, server.RegisterProxy)
	sessionsCertificate := proxy.ca.Issue(t, peertls.SessionsURI)
	serverTLS := &peertls.ClientTLS{
		CAFile: proxy.ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
		CertFile: sessionsCertificate.CertFile, KeyFile: sessionsCertificate.KeyFile, ClientURI: peertls.SessionsURI,
	}
	deliveryHub, err := wakeups.Listen(ctx, integrationDatabase.URL, dispatcher.NotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer deliveryHub.Close()
	worker := &dispatcher.DeliveryWorker{
		Store: store, Catalog: queueCatalog{dispatcher.SandboxSnapshot{
			Name: testSandbox, Namespace: f.namespace, UID: sandboxUID, Incarnation: string(f.pod.UID),
		}}, Wake: queueWake{},
		Delivery: &dispatcher.RuntimeProxyClient{
			Namespace: f.namespace, GatewayTLS: serverTLS, Timeout: 5 * time.Second,
			TargetTemplate: proxy.target,
		},
		Hub: deliveryHub, Ready: make(chan struct{}),
	}
	workerDone := make(chan error, 1)
	go func() { workerDone <- worker.Run(ctx) }()
	select {
	case <-worker.Ready:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}

	select {
	case received := <-runtime.submitted:
		request := received.request
		if request.GetCommandId() != command.GetCommandId() || request.GetSessionId() != "session-queue" || request.GetText() != "persisted input" {
			t.Fatalf("queued request changed before Runtime admission: %v", request)
		}
		if got := received.metadata.Get("agentplane-sandbox-uid"); len(got) != 1 || got[0] != sandboxUID {
			t.Fatalf("Server omitted the Sandbox UID fence: %v", received.metadata)
		}
		if got := received.metadata.Get("agentplane-incarnation-uid"); len(got) != 1 || got[0] != string(f.pod.UID) {
			t.Fatalf("Server omitted the incarnation fence: %v", received.metadata)
		}
		if got := received.metadata.Get("agentplane-environment-runtime-boot-id"); len(got) != 1 || got[0] != runtime.bootID {
			t.Fatalf("Server omitted the Runtime boot fence: %v", received.metadata)
		}
	case <-ctx.Done():
		t.Fatal("queued command did not reach Runtime")
	}
	for {
		row, err := store.GetDispatch(ctx, command.GetCommandId())
		if err != nil {
			t.Fatal(err)
		}
		if row.State == dispatcher.AdmittedState {
			if row.Admission.GetCursor() != 81 {
				t.Fatalf("wrong stored Runtime admission: %+v", row)
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
}

func waitForRuntimeConnection(ctx context.Context, server *Server, sandboxUID string) <-chan struct{} {
	ready := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			if _, err := server.connections.holder(sandboxUID); err == nil {
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

var _ environmentruntimepb.EnvironmentRuntimeServer = (*queueRuntime)(nil)
