package runtimeproxy

import (
	"context"
	"fmt"
	"maps"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
	ctrlcache "sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type observedRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	sandboxUID     string
	incarnationUID string
	bootID         string
	facts          *environmentruntimestatepb.EnvironmentRuntimeFacts
	calls          atomic.Int32
	quiesceCalls   atomic.Int32
}

func (r *observedRuntime) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	if request.GetSandboxUid() != r.sandboxUID || request.GetIncarnationUid() != r.incarnationUID ||
		request.GetObserveOnly() {
		return status.Error(codes.FailedPrecondition, "invalid Runtime observation fence")
	}
	if err := stream.Send(&environmentruntimepb.WatchStateResponse{
		SandboxUid:               request.SandboxUid,
		EnvironmentRuntimeBootId: r.bootID, IncarnationUid: request.IncarnationUid,
		Facts: r.facts,
	}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (r *observedRuntime) validateFence(ctx context.Context) error {
	md, _ := metadata.FromIncomingContext(ctx)
	for key, want := range map[string]string{
		"agentplane-sandbox-uid":                 r.sandboxUID,
		"agentplane-incarnation-uid":             r.incarnationUID,
		"agentplane-environment-runtime-boot-id": r.bootID,
	} {
		got := md.Get(key)
		if len(got) != 1 {
			return status.Errorf(codes.InvalidArgument, "Runtime request requires one %s", key)
		}
		if got[0] != want {
			return status.Errorf(codes.FailedPrecondition, "Runtime request has stale %s", key)
		}
	}
	return nil
}

func (r *observedRuntime) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if err := r.validateFence(ctx); err != nil {
		return nil, err
	}
	r.calls.Add(1)
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

func (r *observedRuntime) Quiesce(ctx context.Context, _ *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	if err := r.validateFence(ctx); err != nil {
		return nil, err
	}
	r.quiesceCalls.Add(1)
	return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}, nil
}

func TestRuntimeObserverPublishesIdentityAndFencesRPCs(t *testing.T) {
	for _, mode := range []peertls.SecurityMode{peertls.MutualTLS, peertls.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRuntimeObserver(t, mode)
		})
	}
}

func TestAnyServerReplicaObservesAndCallsOneRuntime(t *testing.T) {
	for _, uri := range []string{peertls.RuntimeProxyURI} {
		t.Run(uri, func(t *testing.T) { testAnyReplicaObservesAndCallsOneRuntime(t, uri) })
	}
}

func testAnyReplicaObservesAndCallsOneRuntime(t *testing.T, uri string) {
	f := newSandboxFixture(t)
	f.serverTLS = f.ca.Issue(t, uri)
	serverA := f.newServer(t)
	serverB := f.newServer(t)
	destinationA, backend := startProductionRuntime(t, f, serverA)
	destinationB, err := serverB.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	destinationB.target = destinationA.target

	cancelA, doneA, _ := runObserver(t, serverA, destinationA)
	cancelB, doneB, resultB := runObserver(t, serverB, destinationB)
	defer cancelB()
	awaitConnection(t, serverA, string(f.sandbox.UID), "shared-runtime-boot", doneA)
	connectionB := awaitConnection(t, serverB, string(f.sandbox.UID), "shared-runtime-boot", doneB)
	clientA := startProxyClient(t, serverA)
	clientB := startProxyClient(t, serverB)
	if _, err := clientA.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("Server A did not call the shared Runtime: %v", err)
	}
	if _, err := clientB.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("Server B did not call the shared Runtime: %v", err)
	}
	if got := backend.calls.Load(); got != 2 {
		t.Fatalf("shared Runtime received %d calls, want one from each Server", got)
	}

	cancelA()
	waitObserver(t, doneA)
	if _, err := clientB.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("Server B stopped serving after observer A closed: %v", err)
	}
	cancelA, doneA, _ = runObserver(t, serverA, destinationA)
	defer cancelA()
	awaitConnection(t, serverA, string(f.sandbox.UID), "shared-runtime-boot", doneA)
	if _, err := clientB.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("Server B stopped serving after observer A restarted: %v", err)
	}
	if current, err := serverB.connections.holder(string(f.sandbox.UID)); err != nil || current != connectionB {
		t.Fatalf("Server A changed Server B's local connection: %v, %+v", err, current)
	}
	if got := backend.calls.Load(); got != 4 {
		t.Fatalf("shared Runtime received %d calls after observer restart, want 4", got)
	}

	credentials, err := f.serverTLS.ClientCredentials(destinationA.peerURI)
	if err != nil {
		t.Fatal(err)
	}
	directConn, err := grpc.NewClient(destinationA.target, grpc.WithTransportCredentials(credentials), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = directConn.Close() })
	directClient := environmentruntimepb.NewEnvironmentRuntimeClient(directConn)
	staleWatch, err := directClient.WatchState(t.Context(), &environmentruntimepb.WatchStateRequest{
		SandboxUid: string(f.sandbox.UID), IncarnationUid: "replaced-incarnation",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := staleWatch.Recv(); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Runtime accepted a stale incarnation observation: %v", err)
	}
	staleBoot := metadata.AppendToOutgoingContext(
		t.Context(),
		"agentplane-sandbox-uid", string(f.sandbox.UID),
		"agentplane-incarnation-uid", string(f.pod.UID),
		"agentplane-environment-runtime-boot-id", "retired-runtime-boot",
	)
	if _, err := directClient.GetStatus(staleBoot, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Runtime accepted a stale boot fence: %v", err)
	}
	staleIncarnation := metadata.AppendToOutgoingContext(
		t.Context(),
		"agentplane-sandbox-uid", string(f.sandbox.UID),
		"agentplane-incarnation-uid", "replaced-incarnation",
		"agentplane-environment-runtime-boot-id", "shared-runtime-boot",
	)
	if _, err := directClient.GetStatus(staleIncarnation, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Runtime accepted a stale incarnation call: %v", err)
	}
	if got := backend.calls.Load(); got != 4 {
		t.Fatalf("stale direct calls reached the Runtime backend: calls=%d", got)
	}
	select {
	case err := <-resultB:
		t.Fatalf("Server B observer stopped while Server A restarted: %v", err)
	default:
	}
}

type statusOnlyRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	calls atomic.Int32
}

func (r *statusOnlyRuntime) GetStatus(context.Context, *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	r.calls.Add(1)
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

func startProductionRuntime(t *testing.T, f *sandboxFixture, g *Server) (runtimeDestination, *statusOnlyRuntime) {
	t.Helper()
	destination, err := g.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	opened, err := (peertls.Security{Mode: peertls.MutualTLS, TLS: &f.runtimeTLS}).Open(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = opened.Close() })
	backend := &statusOnlyRuntime{}
	changes := make(chan struct{})
	runtimeServer := runtimecontrol.New(runtimecontrol.Config{
		SandboxUID: destination.identity.Uid, BootID: "shared-runtime-boot", ListenAddress: "127.0.0.1:0",
		Security: opened, IsClosing: func() bool { return false }, AdoptIncarnation: func(context.Context, string) error { return nil },
		Facts: func() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
			return &environmentruntimestatepb.EnvironmentRuntimeFacts{
				Bootstrap: environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED,
			}, nil
		},
		Changes: func() <-chan struct{} { return changes },
	}, backend, &harnesshostpb.UnimplementedHarnessHostServer{})
	listener, server, err := runtimeServer.Listen(&executorpb.UnimplementedExecutorServer{})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	destination.target = listener.Addr().String()

	controllerTLS := f.ca.Issue(t, peertls.SandboxControllerURI)
	controllerCredentials, err := controllerTLS.ClientCredentials(destination.peerURI)
	if err != nil {
		t.Fatal(err)
	}
	controllerConn, err := grpc.NewClient(destination.target, grpc.WithTransportCredentials(controllerCredentials), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = controllerConn.Close() })
	controllerCtx, stopController := context.WithCancel(t.Context())
	t.Cleanup(stopController)
	controllerWatch, err := environmentruntimepb.NewEnvironmentRuntimeClient(controllerConn).WatchState(controllerCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: destination.identity.Uid, IncarnationUid: destination.podUID, ObserveOnly: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	bound, err := controllerWatch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if bound.GetSandboxUid() != destination.identity.Uid || bound.GetIncarnationUid() != destination.podUID || bound.GetEnvironmentRuntimeBootId() != "shared-runtime-boot" {
		t.Fatalf("Runtime did not bind its Controller-observed identity: %v", bound)
	}
	return destination, backend
}

func startProxyClient(t *testing.T, g *Server) environmentruntimepb.EnvironmentRuntimeClient {
	t.Helper()
	return environmentruntimepb.NewEnvironmentRuntimeClient(serveServer(t, g, g.RegisterProxy))
}

func testRuntimeObserver(t *testing.T, mode peertls.SecurityMode) {
	t.Helper()
	f := newSandboxFixture(t)
	g := f.newServer(t, peertls.CentralSamplerURI)
	g.config.RunnerSecurity = peertls.Security{Mode: mode}
	runtimeSecurity := peertls.Security{Mode: mode}
	if mode == peertls.MutualTLS {
		g.config.RunnerSecurity.TLS = &f.serverTLS
		runtimeSecurity.TLS = &f.runtimeTLS
	}
	destination, err := g.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	creds, err := runtimeSecurity.ServerCredentials(peertls.RuntimeProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	runtime := &observedRuntime{
		sandboxUID: string(f.sandbox.UID), incarnationUID: string(f.pod.UID), bootID: "test-runner-boot",
		facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{
			Bootstrap:     environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED,
			QuiesceStage:  environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING,
			SetupsRunning: true,
		},
	}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
	executorpb.RegisterExecutorServer(server, &echoForwardExecutor{})
	destination.target = grpcserver.Serve(t, server)
	cancel, done, _ := runObserver(t, g, destination)
	connection := awaitConnection(t, g, string(f.sandbox.UID), "test-runner-boot", done)
	if _, err := connection.runtime.GetStatus(t.Context(), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatal(err)
	}
	if runtime.calls.Load() != 1 {
		t.Fatal("Runtime call did not reach server")
	}
	publicConn := serveServer(t, g, g.RegisterExecutor)
	execCtx, stopExec := context.WithTimeout(context.WithValue(t.Context(), serviceConnectionKey{}, peertls.CentralSamplerURI), 5*time.Second)
	defer stopExec()
	exec, err := executorpb.NewExecutorClient(publicConn).OpenSession(execCtx, &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			SessionId: "test-runner-exec-session",
		},
		RequestId: "open-session-request",
	})
	if err != nil {
		t.Fatal(err)
	}
	opened, err := exec.Recv()
	if err != nil || opened.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED || opened.GetExecutionSessionId() != "forwarded-session" {
		t.Fatalf("Runtime execution session was not forwarded: %v %v", opened, err)
	}
	if opened.GetIncarnationUid() != string(f.pod.UID) {
		t.Fatalf("first open did not bind current incarnation: %v", opened)
	}
	if len(opened.GetNativeFrames()) != 1 || string(opened.GetNativeFrames()[0].GetPayload()) != `{"id":"1","result":{}}` {
		t.Fatalf("Server changed execution evidence: %v", opened.GetNativeFrames())
	}
	unfenced := &executorpb.ExecutionScope{Destination: &identitypb.SandboxDestination{
		Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)},
		Owner:  &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
	}, SessionId: "test-runner-exec-session"}
	resumedID := opened.GetExecutionSessionId()
	resume, err := executorpb.NewExecutorClient(publicConn).OpenSession(execCtx, &executorpb.OpenSessionRequest{Scope: unfenced, ResumeSessionId: &resumedID, RequestId: "unfenced-resume"})
	if err == nil {
		_, err = resume.Recv()
	}
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unfenced resume = %v", err)
	}
	_, err = executorpb.NewExecutorClient(publicConn).StartProcess(execCtx, &executorpb.StartProcessRequest{Scope: unfenced, ExecutionSessionId: resumedID, RequestId: "unfenced-start"})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unfenced unary = %v", err)
	}
	quiescing := f.sandbox.DeepCopy()
	quiescing.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	quiescing.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := f.objects.Status().Update(t.Context(), quiescing); err != nil {
		t.Fatal(err)
	}
	started, err := executorpb.NewExecutorClient(publicConn).StartProcess(execCtx, &executorpb.StartProcessRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
		},
		ExecutionSessionId: opened.GetExecutionSessionId(), RequestId: "start-after-quiesce", ProcessId: "proc-1",
	})
	if err != nil || started.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("existing-session unary call during quiescing = %v, %v", started, err)
	}
	stopExec()
	cancel()
	waitObserver(t, done)
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("closed Runtime connection remained callable: %v", err)
	}
}

func TestExecutionConnectionOnlySignalsVerifiedIncarnationChanges(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t, peertls.CentralSamplerURI)
	server := &executorServer{server: g}
	scope := &executorpb.ExecutionScope{
		Destination: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
		},
		ExpectedIncarnationUid: "previous-incarnation", SessionId: "sampler-session",
	}
	setConnection := func(podUID string) {
		g.connections.accept(&runtimeConnection{
			identity: &sandboxreader.Identity{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)},
			podUID:   podUID, ended: make(chan struct{}),
		})
	}
	setConnection("previous-incarnation")

	_, _, _, err := server.executionConnection(t.Context(), scope, false)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale execution scope = %v, want FailedPrecondition", err)
	}
	for _, detail := range status.Convert(err).Details() {
		if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
			t.Fatalf("stale Server holder carried a rebind detail: %v", detail)
		}
	}
	unbound := proto.Clone(scope).(*executorpb.ExecutionScope)
	unbound.ExpectedIncarnationUid = ""
	if _, _, _, err := server.executionConnection(t.Context(), unbound, true); status.Code(err) != codes.Unavailable {
		t.Fatalf("unbound open used stale Runtime holder: %v", err)
	}
	setConnection(string(f.pod.UID))
	if _, connection, stop, err := server.executionConnection(t.Context(), unbound, true); err != nil {
		t.Fatalf("unbound open did not resolve current Runtime: %v", err)
	} else {
		stop()
		if connection.podUID != string(f.pod.UID) {
			t.Fatalf("bound stale Runtime: %q", connection.podUID)
		}
	}
	_, _, _, err = server.executionConnection(t.Context(), scope, false)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale execution scope with current holder = %v, want FailedPrecondition", err)
	}
	var changed *executorpb.SandboxIncarnationChanged
	for _, detail := range status.Convert(err).Details() {
		if candidate, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
			changed = candidate
		}
	}
	if changed == nil || changed.GetSandboxUid() != string(f.sandbox.UID) || changed.GetIncarnationUid() != string(f.pod.UID) {
		t.Fatalf("stale execution details = %v, want verified Sandbox %q and incarnation %q", status.Convert(err).Details(), f.sandbox.UID, f.pod.UID)
	}

	for _, test := range []struct {
		name   string
		scope  *executorpb.ExecutionScope
		mutate func(*sandboxapi.Sandbox)
		code   codes.Code
	}{
		{name: "invalid scope", scope: &executorpb.ExecutionScope{Destination: scope.Destination, SessionId: scope.SessionId}, code: codes.InvalidArgument},
		{name: "foreign Sandbox", scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: "another-sandbox"}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: scope.ExpectedIncarnationUid, SessionId: scope.SessionId,
		}, code: codes.NotFound},
		{name: "unavailable incarnation", scope: scope, mutate: func(sandbox *sandboxapi.Sandbox) {
			sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
			sandbox.Status.Incarnation = nil
		}, code: codes.Unavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.mutate != nil {
				sandbox := f.sandbox.DeepCopy()
				test.mutate(sandbox)
				if err := f.objects.Status().Update(t.Context(), sandbox); err != nil {
					t.Fatal(err)
				}
			}
			_, _, _, err := server.executionConnection(t.Context(), test.scope, false)
			if status.Code(err) != test.code {
				t.Fatalf("execution scope = %v, want %s", err, test.code)
			}
			for _, detail := range status.Convert(err).Details() {
				if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
					t.Fatalf("rejected request carried a rebind detail: %v", detail)
				}
				if test.name == "invalid scope" || test.name == "foreign Sandbox" {
					if _, ok := detail.(*executorpb.ExecutionEnvironmentFailure); ok {
						t.Fatalf("unverified destination carried environment state: %v", detail)
					}
				}
			}
		})
	}
}

func TestRemovingOldConnectionKeepsItsSuccessor(t *testing.T) {
	identity := &sandboxreader.Identity{Namespace: "test", Name: "box", Uid: "sandbox-uid"}
	connections := newConnections()
	old := &runtimeConnection{identity: identity, ended: make(chan struct{})}
	successor := &runtimeConnection{identity: identity, ended: make(chan struct{})}
	connections.accept(old)
	connections.accept(successor)
	connections.remove(old, errSupersededConnection)
	if current, err := connections.holder(identity.Uid); err != nil || current != successor {
		t.Fatalf("removing an old observer removed its successor: %v, %+v", err, current)
	}
}

func TestExecutionConnectionReportsVerifiedEnvironmentFailures(t *testing.T) {
	for _, test := range []struct {
		name       string
		state      executorpb.ExecutionEnvironmentFailure_State
		code       codes.Code
		newSession bool
		mutate     func(*sandboxFixture) error
	}{
		{
			name: "suspended", state: executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
				sandbox.Status.Incarnation = nil
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "starting", state: executorpb.ExecutionEnvironmentFailure_STATE_STARTING, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.Incarnation = nil
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "not ready", state: executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				pod := f.pod.DeepCopy()
				pod.Status.Conditions[0].Status = corev1.ConditionFalse
				_, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{})
				return err
			},
		},
		{
			name: "quiescing admission", state: executorpb.ExecutionEnvironmentFailure_STATE_QUIESCING, code: codes.FailedPrecondition,
			newSession: true,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
				sandbox.Status.StopStage = sandboxapi.StopStageQuiescing
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "control route unavailable", state: executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE, code: codes.Unavailable,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newSandboxFixture(t)
			if test.mutate != nil {
				if err := test.mutate(f); err != nil {
					t.Fatal(err)
				}
			}
			g := f.newServer(t, peertls.CentralSamplerURI)
			server := &executorServer{server: g}
			scope := &executorpb.ExecutionScope{
				Destination: &identitypb.SandboxDestination{
					Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
				},
				ExpectedIncarnationUid: string(f.pod.UID), SessionId: "sampler-session",
			}
			_, _, _, err := server.executionConnection(t.Context(), scope, test.newSession)
			if status.Code(err) != test.code {
				t.Fatalf("execution connection error=%v, want %s", err, test.code)
			}
			var failure *executorpb.ExecutionEnvironmentFailure
			for _, detail := range status.Convert(err).Details() {
				if candidate, ok := detail.(*executorpb.ExecutionEnvironmentFailure); ok {
					failure = candidate
				}
				if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
					t.Fatalf("environment failure disclosed a replacement incarnation: %v", detail)
				}
			}
			if failure == nil || failure.GetState() != test.state || failure.GetOutcome() != executorpb.ExecutionEnvironmentFailure_OUTCOME_NOT_DISPATCHED {
				t.Fatalf("execution environment details=%v, want state %s and not-dispatched", status.Convert(err).Details(), test.state)
			}
		})
	}
}

type echoForwardExecutor struct {
	executorpb.UnimplementedExecutorServer
}

func (*echoForwardExecutor) OpenSession(request *executorpb.OpenSessionRequest, stream grpc.ServerStreamingServer[executorpb.OpenSessionResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	if len(md.Get("agentplane-environment-runtime-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
		len(md.Get("agentplane-incarnation-uid")) != 1 {
		return status.Error(codes.PermissionDenied, "missing executor fence")
	}
	if request.GetScope().GetExpectedIncarnationUid() != md.Get("agentplane-incarnation-uid")[0] {
		return status.Error(codes.FailedPrecondition, "request did not carry verified Runtime incarnation")
	}
	if err := stream.Send(&executorpb.OpenSessionResponse{
		Status:             &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		ExecutionSessionId: "forwarded-session",
		NativeFrames:       []*executorpb.ExecutionNativeFrame{{Direction: "inbound", Payload: []byte(`{"id":"1","result":{}}`)}},
	}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (*echoForwardExecutor) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("agentplane-environment-runtime-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
		len(md.Get("agentplane-incarnation-uid")) != 1 {
		return nil, status.Error(codes.PermissionDenied, "missing executor fence")
	}
	return &executorpb.StartProcessResponse{
		Status:    &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		ProcessId: request.GetProcessId(),
	}, nil
}

func TestRuntimeCertificateCannotFollowReplacementCredentials(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t)
	destination, err := g.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	if err := g.validateRuntimeDestination(t.Context(), destination); err != nil {
		t.Fatal(err)
	}
	pod := f.pod.DeepCopy()
	pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey] = "test-replacement-credential"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := g.validateRuntimeDestination(t.Context(), destination); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old certificate followed replacement: %v", err)
	}
	newURI, err := workloadid.ID{Namespace: f.namespace, Name: f.sandbox.Name, CredentialID: "test-replacement-credential"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	destination.peerURI = newURI
	if err := g.validateRuntimeDestination(t.Context(), destination); err != nil {
		t.Fatalf("current credential was rejected: %v", err)
	}
}

// reconnectingRuntime replaces its boot after the first observation ends without changing Pod identity.
type reconnectingRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	sandboxUID     string
	incarnationUID string
	observations   atomic.Int32
	disconnect     chan struct{}
}

func (r *reconnectingRuntime) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	if request.GetSandboxUid() != r.sandboxUID || request.GetIncarnationUid() != r.incarnationUID ||
		request.GetObserveOnly() {
		return status.Error(codes.FailedPrecondition, "invalid Runtime observation fence")
	}
	observation := r.observations.Add(1)
	if err := stream.Send(&environmentruntimepb.WatchStateResponse{
		SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
		EnvironmentRuntimeBootId: fmt.Sprintf("boot-%d", observation),
		Facts:                    &environmentruntimestatepb.EnvironmentRuntimeFacts{},
	}); err != nil {
		return err
	}
	if observation == 1 {
		select {
		case <-r.disconnect:
			return status.Error(codes.Unavailable, "runtime restarted")
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (r *reconnectingRuntime) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if got := md.Get("agentplane-sandbox-uid"); len(got) != 1 || got[0] != r.sandboxUID {
		return nil, status.Error(codes.FailedPrecondition, "stale Runtime Sandbox")
	}
	if got := md.Get("agentplane-incarnation-uid"); len(got) != 1 || got[0] != r.incarnationUID {
		return nil, status.Error(codes.FailedPrecondition, "stale Runtime incarnation")
	}
	if got := md.Get("agentplane-environment-runtime-boot-id"); len(got) != 1 || got[0] != fmt.Sprintf("boot-%d", r.observations.Load()) {
		return nil, status.Error(codes.Unavailable, "stale Runtime boot metadata")
	}
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

func TestRuntimeObserverReconnectsBeforeNextDiscoveryAndFencesOldBoot(t *testing.T) {
	f := newSandboxFixture(t)
	pod := f.pod.DeepCopy()
	pod.Status.PodIP = "127.0.0.1"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	g := f.newServer(t)
	g.config.RunnerSecurity = peertls.Security{Mode: peertls.Insecure}
	// The Sandbox's only event: transport loss must trigger its own retry.
	g.RuntimeDiscoveryHandler().OnAdd(f.sandbox, true)
	listener, err := net.Listen("tcp", "127.0.0.1:9090")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runtime := &reconnectingRuntime{
		sandboxUID: string(f.sandbox.UID), incarnationUID: string(f.pod.UID), disconnect: make(chan struct{}),
	}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- g.RunRuntimeObservers(ctx) }()
	defer func() {
		cancel()
		<-finished
	}()
	first := awaitConnection(t, g, string(f.sandbox.UID), "boot-1", ctx.Done())
	close(runtime.disconnect)
	second := awaitConnection(t, g, string(f.sandbox.UID), "boot-2", ctx.Done())
	if second.podUID != first.podUID {
		t.Fatalf("reconnect changed incarnation: first=%+v second=%+v", first, second)
	}
	g.connections.remove(first, errSupersededConnection)
	if _, err := second.runtime.GetStatus(ctx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("replacement Runtime did not serve calls: %v", err)
	}
	stale := &fencedRuntimeConn{conn: second.direct.conn, identity: first.direct.identity, connection: first}
	if _, err := environmentruntimepb.NewEnvironmentRuntimeClient(stale).GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old boot remained usable after reconnect: %v", err)
	}
}

func TestRuntimeObserverKeepsUnchangedDestinationOnSandboxUpdate(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t)
	destination, err := g.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	// A status update causes another read of the same identity into a new protobuf value.
	updated := f.sandbox.DeepCopy()
	updated.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	updated.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := f.objects.Status().Update(t.Context(), updated); err != nil {
		t.Fatal(err)
	}
	oldDone := make(chan struct{})
	cancelled := false
	active := map[string]runtimeObserver{
		f.sandbox.Name: {destination: destination, cancel: func() { cancelled = true }, done: oldDone},
	}
	ctx, cancel := context.WithCancel(t.Context())
	var workers sync.WaitGroup
	defer workers.Wait()
	defer cancel()
	if err := g.reconcileObserver(ctx, f.sandbox.Name, active, &workers); err != nil {
		t.Fatal(err)
	}
	if cancelled || active[f.sandbox.Name].done != oldDone {
		t.Fatal("an unchanged destination replaced the live runtime observer")
	}
}

func TestReconcileWaitsForCanceledObserverBeforeStartingSuccessor(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t)
	oldDestination, err := g.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	updatedPod := f.pod.DeepCopy()
	updatedPod.Status.PodIP = "192.0.2.21"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), updatedPod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	cancelRequested := make(chan struct{})
	oldDone := make(chan struct{})
	active := map[string]runtimeObserver{
		f.sandbox.Name: {
			destination: oldDestination,
			cancel:      func() { close(cancelRequested) },
			done:        oldDone,
		},
	}
	ctx, cancel := context.WithCancel(t.Context())
	var workers sync.WaitGroup
	defer func() {
		cancel()
		workers.Wait()
	}()
	reconciled := make(chan error, 1)
	go func() { reconciled <- g.reconcileObserver(ctx, f.sandbox.Name, active, &workers) }()
	select {
	case <-cancelRequested:
	case <-time.After(time.Second):
		t.Fatal("reconcile did not cancel the old observer")
	}
	select {
	case err := <-reconciled:
		t.Fatalf("reconcile started its successor before the old observer stopped: %v", err)
	default:
	}
	if active[f.sandbox.Name].done != oldDone {
		t.Fatal("reconcile replaced the active observer before it stopped")
	}
	close(oldDone)
	select {
	case err := <-reconciled:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("reconcile did not start a successor after the old observer stopped")
	}
	if active[f.sandbox.Name].destination.target == oldDestination.target {
		t.Fatal("successor kept the old Runtime destination")
	}
}

func TestRuntimeDiscoveryEnqueuesTheSandboxAnEventConcerns(t *testing.T) {
	g := &Server{observerQueue: workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]())}
	defer g.observerQueue.ShutDown()
	handler := g.RuntimeDiscoveryHandler()
	labelled := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "test-runner-pod", Labels: map[string]string{sandboxapi.SandboxLabel: "test-pod-box"}}}
	handler.OnUpdate(labelled, labelled)
	handler.OnAdd(&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "test-unlabelled-pod"}}, false)
	handler.OnDelete(toolscache.DeletedFinalStateUnknown{Obj: &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: "test-deleted-box"}}})
	queued := make(map[string]struct{})
	for g.observerQueue.Len() > 0 {
		name, _ := g.observerQueue.Get()
		queued[name] = struct{}{}
		g.observerQueue.Done(name)
	}
	if !maps.Equal(queued, map[string]struct{}{"test-pod-box": {}, "test-deleted-box": {}}) {
		t.Fatalf("queued Sandboxes = %v", queued)
	}
}

// heartbeatingRuntime reports its state every few milliseconds, far faster than any production runtime.
type heartbeatingRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	heartbeats atomic.Int32
}

func (r *heartbeatingRuntime) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := stream.Send(&environmentruntimepb.WatchStateResponse{
			SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
			EnvironmentRuntimeBootId: "test-heartbeating-boot", Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
		}); err != nil {
			return err
		}
		r.heartbeats.Add(1)
		select {
		case <-stream.Context().Done():
			return stream.Context().Err()
		case <-ticker.C:
		}
	}
}

// countingTransport counts the requests made through it.
type countingTransport struct {
	next     http.RoundTripper
	requests *atomic.Int32
}

func (t countingTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	t.requests.Add(1)
	return t.next.RoundTrip(request)
}

func TestRuntimeHeartbeatsReadNoSandboxOrPodFromTheAPIServer(t *testing.T) {
	f := newSandboxFixture(t)
	pod := f.pod.DeepCopy()
	pod.Status.PodIP = "127.0.0.1"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	counted := rest.CopyConfig(api.Config)
	counted.Wrap(func(next http.RoundTripper) http.RoundTripper {
		return countingTransport{next: next, requests: &requests}
	})
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, sandboxapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	inventory, err := ctrlcache.New(counted, ctrlcache.Options{Scheme: scheme, DefaultNamespaces: map[string]ctrlcache.Config{f.namespace: {}}})
	if err != nil {
		t.Fatal(err)
	}
	health := watchhealth.New()
	g := f.newServerReading(t, inventory, health)
	g.config.RunnerSecurity = peertls.Security{Mode: peertls.Insecure}
	for _, watched := range []client.Object{&sandboxapi.Sandbox{}, &corev1.Pod{}} {
		informer, err := inventory.GetInformer(t.Context(), watched)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := informer.AddEventHandler(g.RuntimeDiscoveryHandler()); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	cacheDone := make(chan error, 1)
	go func() { cacheDone <- inventory.Start(ctx) }()
	if !inventory.WaitForCacheSync(ctx) {
		t.Fatal("cache did not sync")
	}
	health.MarkSynced()
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(sandboxapi.EnvironmentRuntimeServicePort)))
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runtime := &heartbeatingRuntime{}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	finished := make(chan error, 1)
	go func() { finished <- g.RunRuntimeObservers(ctx) }()
	defer func() {
		cancel()
		<-finished
		<-cacheDone
	}()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	awaitHeartbeats := func(count int32) {
		t.Helper()
		for runtime.heartbeats.Load() < count {
			select {
			case <-ctx.Done():
				t.Fatalf("runtime sent %d of %d heartbeats: %v", runtime.heartbeats.Load(), count, ctx.Err())
			case <-poll.C:
			}
		}
	}
	awaitConnection(t, g, string(f.sandbox.UID), "test-heartbeating-boot", ctx.Done())
	baseline := requests.Load()
	awaitHeartbeats(runtime.heartbeats.Load() + 50)
	if added := requests.Load() - baseline; added != 0 {
		t.Fatalf("50 runtime heartbeats made %d Sandbox or Pod API requests", added)
	}
}

// A proxied call can take a connection from holder just before its observer ends it and closes the
// ClientConn; gRPC then fails the call Canceled ("the client connection is closing"), which callers
// do not retry.
func TestCallOnEndedConnectionReportsTheEnd(t *testing.T) {
	conn, err := grpc.NewClient("passthrough:///test-runner.invalid:1", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	connection := &runtimeConnection{ended: make(chan struct{})}
	connection.direct = &fencedRuntimeConn{conn: conn, identity: metadata.MD{}, connection: connection}
	connection.runtime = environmentruntimepb.NewEnvironmentRuntimeClient(connection.direct)
	connection.end(errSupersededConnection)
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.runtime.GetStatus(t.Context(), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("call on an ended connection = %v, want the connection's end (Aborted)", err)
	}
}
