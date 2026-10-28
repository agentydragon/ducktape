package gateway

import (
	"bytes"
	"context"
	"net"
	"sync/atomic"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type observedRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	bootID string
	calls  atomic.Int32
}

func TestReverseControlHasNoBearerAuthenticationFallback(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-auth-gateway", "192.0.2.31")
	listener := newBufconnListener()
	// Even a misregistered plaintext listener must not turn a valid workload bearer into
	// a runner identity. Production additionally requires mTLS at the private listener.
	server := grpc.NewServer()
	g.RegisterRunnerControl(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///test-runner-auth", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	for _, token := range []string{"", f.runnerToken} {
		ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token), time.Second)
		stream, err := runnercontrolpb.NewRunnerControlClient(conn).Connect(ctx)
		if err == nil {
			_, err = stream.Recv()
		}
		cancel()
		if status.Code(err) != codes.Unauthenticated {
			t.Fatalf("runner without a verified certificate was accepted: %v", err)
		}
	}
}

func (r *observedRunner) WatchState(request *runnerservicepb.WatchStateRequest, stream grpc.ServerStreamingServer[runnerservicepb.WatchStateResponse]) error {
	if err := stream.Send(&runnerservicepb.WatchStateResponse{
		SandboxUid:   request.SandboxUid,
		RunnerBootId: r.bootID, IncarnationUid: request.IncarnationUid, ConnectionEpoch: request.ConnectionEpoch,
		Facts: &runnercontrolpb.RunnerFacts{Bootstrap: runnercontrolpb.RunnerFacts_BOOTSTRAP_SUCCEEDED},
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
		return nil, status.Error(codes.PermissionDenied, "missing forward request fence")
	}
	r.calls.Add(1)
	return &runnerpb.RunnerStatus{}, nil
}

func TestForwardObserverPublishesFactsAndFencesRPCsWithoutReverseControl(t *testing.T) {
	f := newSandboxFixture(t)
	pod := f.pod.DeepCopy()
	pod.Labels[sandboxapi.RunnerTransportLabel] = "forward"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	g := f.newGateway(t, "test-forward-gateway", "192.0.2.30", executorAccount)
	destination, err := g.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	creds, err := f.runnerTLS.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	runner := &observedRunner{bootID: "test-forward-boot"}
	runnerservicepb.RegisterRunnerServer(server, runner)
	runnerexecpb.RegisterRunnerExecServer(server, &echoForwardExecutor{})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	destination.target = listener.Addr().String()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- g.observeForward(ctx, destination) }()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	var connection *runnerConnection
	for connection == nil {
		select {
		case err := <-finished:
			t.Fatalf("forward observer stopped: %v", err)
		case <-deadline.C:
			t.Fatal("forward runner never became callable")
		case <-poll.C:
			connection, _ = g.connections.holder(string(f.sandbox.UID))
		}
	}
	if _, err := connection.runner.GetStatus(t.Context(), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatal(err)
	}
	if runner.calls.Load() != 1 {
		t.Fatal("forward call did not reach runner")
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
		ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-forward-exec-session",
	}}}); err != nil {
		t.Fatal(err)
	}
	ready, err := exec.Recv()
	if err != nil || !ready.GetReady().GetAccepted() {
		t.Fatalf("forward executor was not ready: %v %v", ready, err)
	}
	payload := []byte{0, 1, 2, 255}
	if err := exec.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
		Opcode: executorpb.ExecutorMessage_OPCODE_BINARY, Payload: payload,
	}}}); err != nil {
		t.Fatal(err)
	}
	echo, err := exec.Recv()
	if err != nil || !bytes.Equal(echo.GetMessage().GetPayload(), payload) || echo.GetChannelId() != "" {
		t.Fatalf("forward executor changed the message: %v %v", echo, err)
	}
	stopExec()
	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != runner.bootID || lease.Annotations[sandboxlease.PodUIDAnnotation] != string(f.pod.UID) {
		t.Fatalf("forward connection did not publish its identities: %v", lease.Annotations)
	}
	other := f.newGateway(t, "test-other-gateway", "192.0.2.31")
	if _, err := other.records.claimForward(t.Context(), destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("another live Gateway stole forward ownership: %v", err)
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("forward observation did not stop")
	}
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("closed forward connection remained callable: %v", err)
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
	if first.GetOpen() == nil || first.GetChannelId() != "" {
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

func TestRunnerCertificateCannotFollowReplacementCredentialsOrTransport(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-gateway", "192.0.2.31")
	uri, err := f.runnerTLS.LocalIdentity()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := g.certificatePeer(t.Context(), uri, "reverse"); err != nil {
		t.Fatal(err)
	}
	pod := f.pod.DeepCopy()
	pod.Labels[sandboxapi.RunnerCredentialLabel] = "test-replacement-credential"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := g.certificatePeer(t.Context(), uri, "reverse"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old certificate followed replacement: %v", err)
	}
	newURI, err := runner_transport.RunnerURI(f.namespace, string(f.sandbox.UID), "test-replacement-credential")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := g.certificatePeer(t.Context(), newURI, "forward"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("wrong transport was accepted: %v", err)
	}
}
