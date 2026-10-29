package gateway

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"sync/atomic"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
	runnerexecpb.RegisterRunnerExecServer(server, &echoForwardExecutor{})
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
	publicServer := grpc.NewServer()
	g.RegisterExecutorGateway(publicServer)
	go func() { _ = publicServer.Serve(publicListener) }()
	t.Cleanup(publicServer.Stop)
	publicConn, err := grpc.NewClient(publicListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer publicConn.Close()
	execCtx, stopExec := context.WithTimeout(metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Bearer "+f.executorToken)), 5*time.Second)
	defer stopExec()
	exec, err := executorpb.NewExecutorGatewayClient(publicConn).Connect(execCtx)
	if err != nil {
		t.Fatal(err)
	}
	if err := exec.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{
		Destination: &sandboxespb.SandboxDestination{
			Sandbox: f.sandbox.Name, SandboxUid: string(f.sandbox.UID),
			Owner: &sandboxespb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
		},
		ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
	}}}); err != nil {
		t.Fatal(err)
	}
	ready, err := exec.Recv()
	if err != nil || !ready.GetReady().GetAccepted() {
		t.Fatalf("Runner executor was not ready: %v %v", ready, err)
	}
	payload := []byte{0, 1, 2, 255}
	if err := exec.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
		Opcode: executorpb.ExecutorMessage_OPCODE_BINARY, Payload: payload,
	}}}); err != nil {
		t.Fatal(err)
	}
	echo, err := exec.Recv()
	if err != nil || !bytes.Equal(echo.GetMessage().GetPayload(), payload) {
		t.Fatalf("Runner executor changed the message: %v %v", echo, err)
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
	runnerexecpb.UnimplementedRunnerExecServer
}

func (*echoForwardExecutor) Connect(stream grpc.BidiStreamingServer[executorpb.ExecutorFrame, executorpb.ExecutorFrame]) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if first.GetOpen() == nil {
		return status.Error(codes.InvalidArgument, "expected a private executor open")
	}
	md, _ := metadata.FromIncomingContext(stream.Context())
	if len(md.Get("agentplane-runner-boot-id")) != 1 {
		return status.Error(codes.PermissionDenied, "missing executor fence")
	}
	if err := stream.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: true}}}); err != nil {
		return err
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			return err
		}
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
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
	// Discovery is deliberately later than this test's deadline: transport loss must trigger its own retry.
	g.config.ControlTiming.HeartbeatInterval = time.Hour
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
	stale := &fencedRunnerConn{conn: second.direct.conn, identity: first.direct.identity}
	if _, err := runnerservicepb.NewRunnerClient(stale).GetStatus(ctx, &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old boot remained usable after reconnect: %v", err)
	}
}

func TestRunnerDiscoveryWakesOnlyWhenARunnerBecomesReachable(t *testing.T) {
	g := &Gateway{runnerWake: make(chan struct{}, 1)}
	handler := g.RunnerDiscoveryHandler()
	woke := func() bool {
		select {
		case <-g.runnerWake:
			return true
		default:
			return false
		}
	}
	pod := func(ready bool, ip string) *corev1.Pod {
		status := corev1.ConditionFalse
		if ready {
			status = corev1.ConditionTrue
		}
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "runner", Labels: map[string]string{sandboxapi.SandboxLabel: "box"}},
			Status:     corev1.PodStatus{PodIP: ip, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: status}}},
		}
	}
	handler.OnUpdate(pod(false, "10.0.0.1"), pod(false, "10.0.0.1"))
	if woke() {
		t.Fatal("an unchanged Pod woke discovery")
	}
	handler.OnUpdate(pod(false, "10.0.0.1"), pod(true, "10.0.0.1"))
	if !woke() {
		t.Fatal("a Sandbox Pod becoming Ready did not wake discovery")
	}
	unrelated := pod(true, "10.0.0.2")
	unrelated.Labels = nil
	handler.OnAdd(unrelated, false)
	if woke() {
		t.Fatal("a Pod without the Sandbox label woke discovery")
	}
	idle := &sandboxapi.Sandbox{}
	handler.OnUpdate(idle, idle)
	if woke() {
		t.Fatal("a Sandbox without a new incarnation woke discovery")
	}
	started := &sandboxapi.Sandbox{Status: sandboxapi.SandboxStatus{Incarnation: &sandboxapi.Incarnation{Name: "runner", UID: "u1"}}}
	handler.OnUpdate(idle, started)
	if !woke() {
		t.Fatal("a Sandbox gaining an incarnation did not wake discovery")
	}
}
