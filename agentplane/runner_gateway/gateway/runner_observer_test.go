package gateway

import (
	"context"
	"fmt"
	"maps"
	"net"
	"net/http"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"github.com/agentydragon/ducktape/agentplane/executor/rpcerrors"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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

type observedRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	bootID string
	calls  atomic.Int32
}

func (r *observedRunner) WatchState(request *runnerservicepb.WatchStateRequest, stream grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse]) error {
	if err := stream.Send(&runnerservicepb.WatchStateResponse{
		SandboxUid:   request.SandboxUid,
		RunnerBootId: r.bootID, IncarnationUid: request.IncarnationUid, ConnectionEpoch: request.ConnectionEpoch,
		Facts: &runnerstatepb.RunnerFacts{Bootstrap: runnerstatepb.RunnerFacts_BOOTSTRAP_SUCCEEDED},
	}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (r *observedRunner) GetStatus(ctx context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("agentplane-runner-boot-id")) != 1 || md.Get("agentplane-runner-boot-id")[0] != r.bootID ||
		len(md.Get("agentplane-sandbox-uid")) != 1 || len(md.Get("agentplane-incarnation-uid")) != 1 ||
		len(md.Get("agentplane-connection-epoch")) != 1 {
		return nil, status.Error(codes.PermissionDenied, "missing Runner request fence")
	}
	r.calls.Add(1)
	return &runnerpb.RunnerStatus{}, nil
}

func TestRunnerObserverPublishesFactsAndFencesRPCs(t *testing.T) {
	for _, mode := range []runner_transport.SecurityMode{runner_transport.MutualTLS, runner_transport.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRunnerObserver(t, mode)
		})
	}
}

func TestOpenSessionAdmissionFailureCarriesNotDispatchedEvidence(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30")
	stream := &openSessionProbeStream{ctx: t.Context()}
	request := &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &sandboxespb.SandboxDestination{
				Sandbox: f.sandbox.Name, SandboxUid: string(f.sandbox.UID),
				Owner: &sandboxespb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID),
			SessionId:              "test-open-session-admission",
		},
		RequestId: "test-open-session-admission-request",
	}
	err := (&executorServer{gateway: g}).OpenSession(request, stream)
	if status.Code(err) != codes.Unavailable || !rpcerrors.IsOpenSessionNotDispatched(err) {
		t.Fatalf("OpenSession admission error=%v, want structured not-dispatched Unavailable", err)
	}
	if stream.sent {
		t.Fatal("Gateway sent an OpenSession response after admission failed")
	}
}

type openSessionProbeStream struct {
	grpc.ServerStream
	ctx  context.Context
	sent bool
}

func (s *openSessionProbeStream) Context() context.Context { return s.ctx }

func (s *openSessionProbeStream) Send(*executorpb.OpenSessionResponse) error {
	s.sent = true
	return nil
}

func testRunnerObserver(t *testing.T, mode runner_transport.SecurityMode) {
	t.Helper()
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	g.config.RunnerSecurity = runner_transport.Security{Mode: mode}
	runnerSecurity := runner_transport.Security{Mode: mode}
	if mode == runner_transport.MutualTLS {
		g.config.RunnerSecurity.TLS = &f.gatewayTLS
		runnerSecurity.TLS = &f.runnerTLS
	}
	destination, err := g.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	creds, err := runnerSecurity.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	runner := &observedRunner{bootID: "test-runner-boot"}
	runnerservicepb.RegisterRunnerServer(server, runner)
	executorpb.RegisterExecutorServer(server, &echoForwardExecutor{})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	destination.target = listener.Addr().String()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- g.observeRunner(ctx, destination) }()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	var connection *runnerConnection
	for connection == nil {
		select {
		case err := <-finished:
			t.Fatalf("Runner observer stopped: %v", err)
		case <-deadline.C:
			t.Fatal("Runner never became callable")
		case <-poll.C:
			connection, _ = g.connections.holder(string(f.sandbox.UID))
		}
	}
	if _, err := connection.runner.GetStatus(t.Context(), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatal(err)
	}
	if runner.calls.Load() != 1 {
		t.Fatal("Runner call did not reach server")
	}
	publicListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	publicServer := grpc.NewServer(g.Methods().ServerOptions(0)...)
	g.RegisterExecutor(publicServer)
	go func() { _ = publicServer.Serve(publicListener) }()
	t.Cleanup(publicServer.Stop)
	publicConn, err := grpc.NewClient(publicListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer publicConn.Close()
	execCtx, stopExec := context.WithTimeout(metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Bearer "+f.executorToken)), 5*time.Second)
	defer stopExec()
	client := executorpb.NewExecutorClient(publicConn)
	openRequest := &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &sandboxespb.SandboxDestination{
				Sandbox: f.sandbox.Name, SandboxUid: string(f.sandbox.UID),
				Owner: &sandboxespb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
		},
		RequestId: "open-session-request",
	}
	exec, err := client.OpenSession(execCtx, openRequest)
	if err != nil {
		t.Fatal(err)
	}
	opened, err := exec.Recv()
	if err != nil || opened.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED || opened.GetExecutionSessionId() != "forwarded-session" {
		t.Fatalf("Runner execution session was not forwarded: %v %v", opened, err)
	}
	if len(opened.GetNativeFrames()) != 1 || string(opened.GetNativeFrames()[0].GetPayload()) != `{"id":"1","result":{}}` {
		t.Fatalf("Gateway changed execution evidence: %v", opened.GetNativeFrames())
	}
	openRequest.RequestId = "runner-unavailable"
	failedExec, err := client.OpenSession(execCtx, openRequest)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := failedExec.Recv(); status.Code(err) != codes.Unavailable || rpcerrors.IsOpenSessionNotDispatched(err) {
		t.Fatalf("downstream Runner OpenSession error=%v, want unmarked Unavailable", err)
	} else {
		var preservesOtherDetail bool
		for _, detail := range status.Convert(err).Details() {
			if _, ok := detail.(*errdetails.BadRequest); ok {
				preservesOtherDetail = true
			}
		}
		if !preservesOtherDetail {
			t.Fatalf("stripping downstream marker discarded unrelated status details: %v", err)
		}
	}
	quiescing := f.sandbox.DeepCopy()
	quiescing.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	quiescing.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := f.objects.Status().Update(t.Context(), quiescing); err != nil {
		t.Fatal(err)
	}
	started, err := executorpb.NewExecutorClient(publicConn).StartProcess(execCtx, &executorpb.StartProcessRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &sandboxespb.SandboxDestination{
				Sandbox: f.sandbox.Name, SandboxUid: string(f.sandbox.UID),
				Owner: &sandboxespb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
		},
		ExecutionSessionId: opened.GetExecutionSessionId(), RequestId: "start-after-quiesce", ProcessId: "proc-1",
	})
	if err != nil || started.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("existing-session unary call during quiescing = %v, %v", started, err)
	}
	stopExec()
	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != runner.bootID || lease.Annotations[sandboxlease.PodUIDAnnotation] != string(f.pod.UID) {
		t.Fatalf("Runner connection did not publish its identities: %v", lease.Annotations)
	}
	other := f.newGateway(t, "test-other-gateway", "192.0.2.31")
	if _, err := other.records.claim(t.Context(), destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("another live Gateway stole Runner ownership: %v", err)
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("Runner observation did not stop")
	}
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("closed Runner connection remained callable: %v", err)
	}
}

type echoForwardExecutor struct {
	executorpb.UnimplementedExecutorServer
}

func (*echoForwardExecutor) OpenSession(request *executorpb.OpenSessionRequest, stream grpc.ServerStreamingServer[executorpb.OpenSessionResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	if len(md.Get("agentplane-runner-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
		len(md.Get("agentplane-incarnation-uid")) != 1 || len(md.Get("agentplane-connection-epoch")) != 1 {
		return status.Error(codes.PermissionDenied, "missing executor fence")
	}
	if request.GetRequestId() == "runner-unavailable" {
		marked := rpcerrors.WithOpenSessionNotDispatched(status.Error(codes.Unavailable, "downstream Runner unavailable"))
		markedStatus, _ := status.FromError(marked)
		markedStatus, err := markedStatus.WithDetails(&errdetails.BadRequest{FieldViolations: []*errdetails.BadRequest_FieldViolation{{Field: "fixture", Description: "preserve this detail"}}})
		if err != nil {
			return err
		}
		return markedStatus.Err()
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
	if len(md.Get("agentplane-runner-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
		len(md.Get("agentplane-incarnation-uid")) != 1 || len(md.Get("agentplane-connection-epoch")) != 1 {
		return nil, status.Error(codes.PermissionDenied, "missing executor fence")
	}
	return &executorpb.StartProcessResponse{
		Status:    &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		ProcessId: request.GetProcessId(),
	}, nil
}

func TestRunnerCertificateCannotFollowReplacementCredentials(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-gateway", "192.0.2.31")
	destination, err := g.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	if err := g.validateRunnerDestination(t.Context(), destination); err != nil {
		t.Fatal(err)
	}
	pod := f.pod.DeepCopy()
	pod.Labels[sandboxapi.RunnerCredentialLabel] = "test-replacement-credential"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := g.validateRunnerDestination(t.Context(), destination); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old certificate followed replacement: %v", err)
	}
	newURI, err := runner_transport.RunnerURI(f.namespace, string(f.sandbox.UID), "test-replacement-credential")
	if err != nil {
		t.Fatal(err)
	}
	destination.peerURI = newURI
	if err := g.validateRunnerDestination(t.Context(), destination); err != nil {
		t.Fatalf("current credential was rejected: %v", err)
	}
}

// reconnectingRunner replaces its boot after the first observation ends without changing Pod identity.
type reconnectingRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	observations atomic.Int32
	disconnect   chan struct{}
}

func (r *reconnectingRunner) WatchState(request *runnerservicepb.WatchStateRequest, stream grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse]) error {
	observation := r.observations.Add(1)
	if err := stream.Send(&runnerservicepb.WatchStateResponse{
		SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
		ConnectionEpoch: request.ConnectionEpoch, RunnerBootId: fmt.Sprintf("boot-%d", observation),
		Facts: &runnerstatepb.RunnerFacts{},
	}); err != nil {
		return err
	}
	if observation == 1 {
		select {
		case <-r.disconnect:
			return status.Error(codes.Unavailable, "runner restarted")
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (r *reconnectingRunner) GetStatus(ctx context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if got := md.Get("agentplane-runner-boot-id"); len(got) != 1 || got[0] != fmt.Sprintf("boot-%d", r.observations.Load()) {
		return nil, status.Error(codes.Unavailable, "stale runner boot")
	}
	return &runnerpb.RunnerStatus{}, nil
}

func TestRunnerObserverReconnectsBeforeNextDiscoveryAndFencesOldBoot(t *testing.T) {
	f := newSandboxFixture(t)
	pod := f.pod.DeepCopy()
	pod.Status.PodIP = "127.0.0.1"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	g := f.newGateway(t, "test-reconnecting-gateway", "192.0.2.30")
	g.config.RunnerSecurity = runner_transport.Security{Mode: runner_transport.Insecure}
	// The Sandbox's only event: transport loss must trigger its own retry.
	g.RunnerDiscoveryHandler().OnAdd(f.sandbox, true)
	listener, err := net.Listen("tcp", "127.0.0.1:9090")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runner := &reconnectingRunner{disconnect: make(chan struct{})}
	runnerservicepb.RegisterRunnerServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- g.RunRunnerObservers(ctx) }()
	defer func() {
		cancel()
		<-finished
	}()
	awaitBoot := func(bootID string) *runnerConnection {
		t.Helper()
		poll := time.NewTicker(time.Millisecond)
		defer poll.Stop()
		for {
			connection, err := g.connections.holder(string(f.sandbox.UID))
			if err == nil && connection.bootID == bootID {
				return connection
			}
			select {
			case <-ctx.Done():
				t.Fatalf("Runner did not publish %s before the next discovery: %v", bootID, ctx.Err())
			case <-poll.C:
			}
		}
	}
	first := awaitBoot("boot-1")
	close(runner.disconnect)
	second := awaitBoot("boot-2")
	if second.epoch <= first.epoch || second.podUID != first.podUID {
		t.Fatalf("reconnect changed incarnation or failed to advance epoch: first=%+v second=%+v", first, second)
	}
	g.connections.remove(first, errSupersededConnection)
	if _, err := second.runner.GetStatus(ctx, &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatalf("replacement Runner did not serve calls: %v", err)
	}
	stale := &fencedRunnerConn{conn: second.direct.conn, identity: first.direct.identity, connection: first}
	if _, err := runnerservicepb.NewRunnerClient(stale).GetStatus(ctx, &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old boot remained usable after reconnect: %v", err)
	}
}

func TestRunnerDiscoveryEnqueuesTheSandboxAnEventConcerns(t *testing.T) {
	g := &Gateway{observerQueue: workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]())}
	defer g.observerQueue.ShutDown()
	handler := g.RunnerDiscoveryHandler()
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

// heartbeatingRunner reports its state every few milliseconds, far faster than any production runner.
type heartbeatingRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	heartbeats atomic.Int32
}

func (r *heartbeatingRunner) WatchState(request *runnerservicepb.WatchStateRequest, stream grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse]) error {
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := stream.Send(&runnerservicepb.WatchStateResponse{
			SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid, ConnectionEpoch: request.ConnectionEpoch,
			RunnerBootId: "test-heartbeating-boot", Facts: &runnerstatepb.RunnerFacts{},
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

func TestRunnerHeartbeatsReadNoSandboxOrPodFromTheAPIServer(t *testing.T) {
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
	g := f.newGatewayReading(t, inventory, health, "test-cached-gateway", "192.0.2.32")
	g.config.RunnerSecurity = runner_transport.Security{Mode: runner_transport.Insecure}
	for _, watched := range []client.Object{&sandboxapi.Sandbox{}, &corev1.Pod{}} {
		informer, err := inventory.GetInformer(t.Context(), watched)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := informer.AddEventHandler(g.RunnerDiscoveryHandler()); err != nil {
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
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(sandboxapi.RunnerServicePort)))
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runner := &heartbeatingRunner{}
	runnerservicepb.RegisterRunnerServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	finished := make(chan error, 1)
	go func() { finished <- g.RunRunnerObservers(ctx) }()
	defer func() {
		cancel()
		<-finished
		<-cacheDone
	}()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	awaitHeartbeats := func(count int32) {
		t.Helper()
		for runner.heartbeats.Load() < count {
			select {
			case <-ctx.Done():
				t.Fatalf("runner sent %d of %d heartbeats: %v", runner.heartbeats.Load(), count, ctx.Err())
			case <-poll.C:
			}
		}
	}
	for {
		if _, err := g.connections.holder(string(f.sandbox.UID)); err == nil {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatalf("Runner never became callable: %v", ctx.Err())
		case <-poll.C:
		}
	}
	baseline := requests.Load()
	awaitHeartbeats(runner.heartbeats.Load() + 50)
	if added := requests.Load() - baseline; added != 0 {
		t.Fatalf("50 runner heartbeats made %d Sandbox or Pod API requests", added)
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
	connection := &runnerConnection{ended: make(chan struct{})}
	connection.direct = &fencedRunnerConn{conn: conn, identity: metadata.MD{}, connection: connection}
	connection.runner = runnerservicepb.NewRunnerClient(connection.direct)
	connection.end(errSupersededConnection)
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.runner.GetStatus(t.Context(), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("call on an ended connection = %v, want the connection's end (Aborted)", err)
	}
}
