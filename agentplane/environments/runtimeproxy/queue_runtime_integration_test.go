package runtimeproxy

import (
	"context"
	"database/sql"
	"net"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
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
	request  *harnesshostpb.SubmitInputRequest
	metadata metadata.MD
}

type queuedRuntimeSession struct {
	request  *harnesshostpb.CreateSessionRequest
	metadata metadata.MD
}

type queueRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	bootID    string
	submitted chan queuedRuntimeRequest
	created   chan queuedRuntimeSession
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

type queueHarnessHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	runtime *queueRuntime
}

func (r *queueHarnessHost) CreateSession(ctx context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	r.runtime.created <- queuedRuntimeSession{request: request, metadata: md}
	return &harnesshostpb.CreateSessionResponse{}, nil
}

func (r *queueHarnessHost) SubmitInput(ctx context.Context, request *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	r.runtime.submitted <- queuedRuntimeRequest{request: request, metadata: md}
	return &harnesshostpb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 81}}, nil
}

// This test queues work through authenticated Dispatcher RPCs and observes the Runtime's
// durable admission through the API, including the worker's native Session materialization.
func TestQueuedDeliveryThroughRuntimeProxy(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	const sessionID = "c31d2498-a1bf-40cd-8262-c54fcf3289ce"
	const commandID = "queue-runner-command"
	f := newSandboxFixture(t)
	sandboxUID := string(f.sandbox.UID)
	catalog := queueCatalog{dispatcher.SandboxSnapshot{
		Name: testSandbox, Namespace: f.namespace, UID: sandboxUID, Incarnation: string(f.pod.UID),
	}}
	service := dispatcher.NewService(db, 4)
	service.Sandboxes = catalog
	service.DispatcherServices = sets.New(peertls.AppURI)
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLS(t, peertls.SessionDispatcherURI, dispatcher.Methods(rule), func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	}, peertls.AppURI)
	client := dispatcherpb.NewSessionDispatcherClient(grpcmethods.AsCaller(connection, peertls.AppURI))
	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: testSandbox, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
	if _, err := client.CreateSession(ctx, &dispatcherpb.CreateSessionRequest{SessionId: sessionID, Spec: spec}); err != nil {
		t.Fatal(err)
	}
	queued, err := client.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{SessionId: sessionID, CommandId: commandID, Text: "persisted input", ExpectedSandboxUid: sandboxUID})
	if err != nil || queued.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER || queued.GetDispatch().GetAdmission() != nil {
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
	runtime := &queueRuntime{bootID: "queue-runner-boot", submitted: make(chan queuedRuntimeRequest, 1), created: make(chan queuedRuntimeSession, 1)}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(runtimeServer, runtime)
	harnesshostpb.RegisterHarnessHostServer(runtimeServer, &queueHarnessHost{runtime: runtime})
	go func() { _ = runtimeServer.Serve(runtimeListener) }()
	defer runtimeServer.Stop()
	runtimeDestination.target = runtimeListener.Addr().String()

	_, observerDone, _ := runObserver(t, server, runtimeDestination)
	awaitConnection(t, server, sandboxUID, runtime.bootID, observerDone)

	proxy := serveServer(t, server, server.RegisterProxy)
	sessionsCertificate := proxy.ca.Issue(t, peertls.SessionsURI)
	serverTLS := &peertls.ClientTLS{
		CAFile: proxy.ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
		CertFile: sessionsCertificate.CertFile, KeyFile: sessionsCertificate.KeyFile, ClientURI: peertls.SessionsURI,
	}
	worker, err := service.NewDeliveryWorker(ctx, integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	worker.Catalog = catalog
	worker.Wake = queueWake{}
	worker.Delivery = &dispatcher.HarnessHostClient{
		Namespace: f.namespace, GatewayTLS: serverTLS, Timeout: 5 * time.Second,
		TargetTemplate: proxy.target,
	}
	workerDone := make(chan error, 1)
	go func() { workerDone <- worker.Run(ctx) }()
	select {
	case <-worker.Ready:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}

	select {
	case created := <-runtime.created:
		if created.request.GetSessionId() != sessionID || created.request.GetExecutionIdentity().GetName() != "queue-identity" || created.request.GetExecutionIdentity().GetUid() != queueIdentityUID {
			t.Fatalf("materialized Session lost its execution Identity: %v", created.request)
		}
		for key, value := range map[string]string{
			"agentplane-sandbox-uid":                 sandboxUID,
			"agentplane-incarnation-uid":             string(f.pod.UID),
			"agentplane-environment-runtime-boot-id": runtime.bootID,
		} {
			if got := created.metadata.Get(key); len(got) != 1 || got[0] != value {
				t.Fatalf("Session materialization fence %s = %v, want %s", key, got, value)
			}
		}
	case <-ctx.Done():
		t.Fatal("queued Session did not reach Runtime")
	}

	select {
	case received := <-runtime.submitted:
		request := received.request
		if request.GetCommandId() != commandID || request.GetSessionId() != sessionID || request.GetText() != "persisted input" {
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
		response, err := client.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: commandID})
		row := response.GetDispatch()
		if err != nil {
			t.Fatal(err)
		}
		if row.GetState() == dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED {
			if row.GetAdmission().GetCursor() != 81 {
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

var _ environmentruntimepb.EnvironmentRuntimeServer = (*queueRuntime)(nil)
