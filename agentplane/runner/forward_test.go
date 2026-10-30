package runner

import (
 "github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	runner_execpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	runnertransporttesting "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestForwardRunnerServesHealthAndFencesStateAndDirectCalls(t *testing.T) {
	for _, mode := range []runnertransport.SecurityMode{runnertransport.MutualTLS, runnertransport.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRunnerHealthAndFencing(t, mode)
		})
	}
}

func testRunnerHealthAndFencing(t *testing.T, mode runnertransport.SecurityMode) {
	t.Helper()
	runnerSecurity := runnertransport.Security{Mode: mode}
	gatewaySecurity := runnertransport.Security{Mode: mode}
	var runnerURI string
	if mode == runnertransport.MutualTLS {
		ca := runnertransporttesting.NewCA(t)
		var err error
		runnerURI, err = runnertransport.RunnerURI("test", "sandbox-uid", "credential-id")
		if err != nil {
			t.Fatal(err)
		}
		runnerTLS := ca.Issue(t, runnerURI)
		gatewayTLS := ca.Issue(t, runnertransport.GatewayURI)
		runnerSecurity.TLS, gatewaySecurity.TLS = &runnerTLS, &gatewayTLS
	}
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	config.Control = configuration.RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", Security: runnerSecurity}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorRelay)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	credentials, err := gatewaySecurity.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	health, err := grpc_health_v1.NewHealthClient(conn).Check(ctx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil || health.GetStatus() != grpc_health_v1.HealthCheckResponse_SERVING {
		t.Fatalf("pre-watch health = %v, error %v; want SERVING", health, err)
	}

	watch, err := runnerservicepb.NewRunnerClient(conn).WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1", ConnectionEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if first.GetRunnerBootId() == "" || first.GetSandboxUid() != "sandbox-uid" || first.GetIncarnationUid() != "pod-uid-1" || first.GetConnectionEpoch() != 1 || first.GetFacts() == nil {
		t.Fatalf("first WatchState response has invalid identity or facts: %v", first)
	}

	callContext := func(incarnation, bootID string, epoch uint64) context.Context {
		return metadata.AppendToOutgoingContext(
			ctx,
			control.SandboxMetadata, "sandbox-uid",
			control.IncarnationMetadata, incarnation,
			control.BootMetadata, bootID,
			control.EpochMetadata, uint64String(epoch),
		)
	}
	statusClient := runnerservicepb.NewRunnerClient(conn)
	if _, err := statusClient.GetStatus(ctx, &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("GetStatus without fencing metadata = %v, want Unauthenticated", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", "old-runner-boot", 1), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus for old runner boot = %v, want Unavailable", err)
	}
	if _, err := statusClient.SubmitInput(callContext("pod-uid-1", "old-runner-boot", 1), &runnerservicepb.SubmitInputRequest{
		SessionId: "not-created", CommandId: "stale-boot-input", Text: "must not be admitted",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput for old runner boot = %v, want retryable rejection before admission", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetRunnerBootId(), 1), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatalf("current-epoch GetStatus: %v", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetRunnerBootId(), 2), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("stale GetStatus error = %v, want Unavailable", err)
	}

	newWatch, err := runnerservicepb.NewRunnerClient(conn).WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-2", ConnectionEpoch: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := newWatch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if second.GetIncarnationUid() != "pod-uid-2" || second.GetConnectionEpoch() != 2 {
		t.Fatalf("successor WatchState response = %v", second)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-2", second.GetRunnerBootId(), 2), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatalf("successor watcher was deactivated by its predecessor: %v", err)
	}
}

func TestRunnerRejectsControlCertificateForAnotherSandbox(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	wrongURI, err := runnertransport.RunnerURI("another-namespace", "another-sandbox", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	wrongTLS := ca.Issue(t, wrongURI)
	config := Config{
		StateDir: t.TempDir(),
		Launch:   configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}},
		Control: configuration.RunnerControlConfiguration{
			ListenAddress: "127.0.0.1:8090", Security: runnertransport.Security{Mode: runnertransport.MutualTLS, TLS: &wrongTLS},
		},
	}
	if _, err := New(config); err == nil {
		t.Fatal("Runner accepted a control certificate for another Sandbox")
	}
}

func TestForwardRunnerRetriesSameAdmissionAfterLostReplyAndWatchCancellation(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runnertransport.RunnerURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, runnertransport.GatewayURI)
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.Control = configuration.RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", Security: runnertransport.Security{Mode: runnertransport.MutualTLS, TLS: &runnerTLS}}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorRelay)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	credentials, err := gatewayTLS.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	var cancelFirst context.CancelFunc
	dropFirstReply := true
	unaryInterceptor := func(
		callCtx context.Context,
		method string,
		request, response any,
		connection *grpc.ClientConn,
		invoke grpc.UnaryInvoker,
		options ...grpc.CallOption,
	) error {
		err := invoke(callCtx, method, request, response, connection, options...)
		if method == "/agentplane.runner.Runner/SubmitInput" && err == nil && dropFirstReply {
			dropFirstReply = false
			cancelFirst()
			return status.Error(codes.Unavailable, "simulated lost Runner admission reply")
		}
		return err
	}
	conn, err := grpc.NewClient(
		listener.Addr().String(),
		grpc.WithTransportCredentials(credentials),
		grpc.WithUnaryInterceptor(unaryInterceptor),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client := runnerservicepb.NewRunnerClient(conn)
	watchCtx, cancelWatch := context.WithCancel(ctx)
	watch, err := client.WatchState(watchCtx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ConnectionEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	callContext := func(parent context.Context, epoch uint64, bootID string) context.Context {
		return metadata.AppendToOutgoingContext(
			parent,
			control.SandboxMetadata, "sandbox-uid",
			control.IncarnationMetadata, "pod-uid",
			control.BootMetadata, bootID,
			control.EpochMetadata, uint64String(epoch),
		)
	}
	if _, err := client.OpenSession(callContext(ctx, 1, first.GetRunnerBootId()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	submitCtx, cancelSubmit := context.WithCancel(ctx)
	cancelFirst = cancelSubmit
	if _, err := client.SubmitInput(callContext(submitCtx, 1, first.GetRunnerBootId()), &runnerservicepb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "same-command", Text: "hello",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("first direct SubmitInput error = %v, want the simulated lost reply", err)
	}
	if submitCtx.Err() != context.Canceled {
		t.Fatalf("first SubmitInput context error = %v, want cancellation after admission", submitCtx.Err())
	}
	cancelSubmit()
	session := r.sessions.Get("session-1")
	firstRecord, err := session.EventJournal().GetCommand(ctx, "same-command")
	if err != nil || firstRecord == nil || firstRecord.AdmittedCursor == 0 {
		t.Fatalf("durable admission after lost reply = %v, %v", firstRecord, err)
	}
	cancelWatch()
	deadline := time.Now().Add(time.Second)
	for {
		_, err := client.GetStatus(callContext(ctx, 1, first.GetRunnerBootId()), &runnerpb.GetStatusRequest{})
		if status.Code(err) == codes.Unavailable {
			break
		}
		if err != nil {
			t.Fatalf("GetStatus while waiting for canceled WatchState = %v", err)
		}
		if time.Now().After(deadline) {
			t.Fatal("GetStatus remained available after WatchState cancellation")
		}
	}
	watch, err = client.WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ConnectionEpoch: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	retry, err := client.SubmitInput(callContext(ctx, 2, second.GetRunnerBootId()), &runnerservicepb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "same-command", Text: "hello",
	})
	if err != nil {
		t.Fatalf("retry with the admitted command ID: %v", err)
	}
	secondRecord, err := session.EventJournal().GetCommand(ctx, "same-command")
	if err != nil || secondRecord == nil || secondRecord.AdmittedCursor != firstRecord.AdmittedCursor ||
		retry.GetAdmission().GetCursor() != firstRecord.AdmittedCursor {
		t.Fatalf("retry admission = %v, record %v, original %v, error %v", retry.GetAdmission(), secondRecord, firstRecord, err)
	}
}

func TestForwardRunnerBoundsDirectExecutorStreams(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runnertransport.RunnerURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, runnertransport.GatewayURI)
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	config.Control = configuration.RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", Security: runnertransport.Security{Mode: runnertransport.MutualTLS, TLS: &runnerTLS}}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorRelay)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	credentials, err := gatewayTLS.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	watch, err := runnerservicepb.NewRunnerClient(conn).WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ConnectionEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	callContext := metadata.AppendToOutgoingContext(
		ctx,
		control.SandboxMetadata, "sandbox-uid",
		control.IncarnationMetadata, "pod-uid",
		control.BootMetadata, first.GetRunnerBootId(),
		control.EpochMetadata, "1",
	)
	executorClient := runner_execpb.NewRunnerExecClient(conn)
	refused, err := executorClient.Connect(callContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := refused.Send(&executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: "session-1"}},
	}); err != nil {
		t.Fatalf("send direct executor open: %v", err)
	}
	ready, err := refused.Recv()
	if err != nil || ready.GetReady() == nil || ready.GetReady().GetAccepted() {
		t.Fatalf("unconfigured executor response = %v, %v; want rejected Ready", ready, err)
	}
	if _, err := refused.Recv(); err != io.EOF {
		t.Fatalf("rejected executor stream ended with %v, want EOF", err)
	}

	const maxExecutorStreams = 8
	closeStreams := make([]func() error, 0, maxExecutorStreams)
	defer func() {
		for _, closeStream := range closeStreams {
			_ = closeStream()
		}
	}()
	for i := 0; i < maxExecutorStreams; i++ {
		stream, err := executorClient.Connect(callContext)
		if err != nil {
			t.Fatalf("open direct executor stream %d: %v", i, err)
		}
		if _, err := stream.Header(); err != nil {
			t.Fatalf("wait for pending direct executor stream %d: %v", i, err)
		}
		closeStreams = append(closeStreams, stream.CloseSend)
	}
	tooManyCtx, cancelTooMany := context.WithTimeout(callContext, time.Second)
	defer cancelTooMany()
	tooMany, err := executorClient.Connect(tooManyCtx)
	if err != nil {
		if status.Code(err) == codes.ResourceExhausted {
			return
		}
		t.Fatalf("open excess direct executor stream: %v", err)
	}
	if _, err := tooMany.Header(); status.Code(err) != codes.ResourceExhausted {
		// Header may be empty even when the server rejects a stream before sending metadata;
		// Recv observes the final gRPC status in that case.
		if _, recvErr := tooMany.Recv(); status.Code(recvErr) != codes.ResourceExhausted {
			t.Fatalf("excess direct executor stream errors = header %v, receive %v; want ResourceExhausted", err, recvErr)
		}
	}
}

func TestForwardRunnerBackpressuresExecutorOutputBurst(t *testing.T) {
	const burst = 100
	start := make(chan struct{})
	secondStart := make(chan struct{})
	finish := make(chan struct{})
	sent := make(chan error, 2)
	execServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			sent <- err
			return
		}
		defer connection.CloseNow()
		select {
		case <-start:
		case <-finish:
			return
		}
		writeBurst := func() error {
			for i := range burst {
				if err := connection.Write(request.Context(), websocket.MessageBinary, bytes.Repeat([]byte{byte(i)}, 1024)); err != nil {
					return err
				}
			}
			return nil
		}
		if err := writeBurst(); err != nil {
			sent <- err
			return
		}
		sent <- nil
		select {
		case <-secondStart:
		case <-finish:
			return
		}
		sent <- writeBurst()
		<-finish
	}))
	defer execServer.Close()
	defer close(finish)
	r := newExecutorLifecycleRunnerWithURL(t, "ws"+strings.TrimPrefix(execServer.URL, "http"), func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	listener, server, err := r.control.Listen(r.executorRelay)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	credentials, err := r.config.Control.Security.ClientCredentials("")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(t.Context(), codexExecServerTestTimeout)
	defer cancel()
	watch, err := runnerservicepb.NewRunnerClient(conn).WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ConnectionEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	callContext := metadata.AppendToOutgoingContext(
		ctx,
		control.SandboxMetadata, "sandbox-uid", control.IncarnationMetadata, "pod-uid",
		control.BootMetadata, first.GetRunnerBootId(), control.EpochMetadata, "1",
	)
	stream, err := runner_execpb.NewRunnerExecClient(conn).Connect(callContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: "session-1"}}}); err != nil {
		t.Fatal(err)
	}
	ready, err := stream.Recv()
	if err != nil || !ready.GetReady().GetAccepted() {
		t.Fatalf("executor readiness = %v, %v", ready, err)
	}
	close(start)
	select {
	case err := <-sent:
		if err != nil {
			t.Fatalf("executor produced burst: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("executor output burst did not reach Runner")
	}
	for i := range burst {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatalf("receive burst frame %d: %v", i, err)
		}
		if message := frame.GetMessage(); message == nil || !bytes.Equal(message.GetPayload(), bytes.Repeat([]byte{byte(i)}, 1024)) {
			t.Fatalf("burst frame %d = %v", i, frame)
		}
	}
	close(secondStart)
	select {
	case err := <-sent:
		if err != nil {
			t.Fatalf("executor produced second burst: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("second executor output burst did not reach Runner")
	}
	cancel()
	stopped := make(chan struct{})
	go func() {
		server.GracefulStop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(codexExecServerTestTimeout):
		server.Stop()
		t.Fatal("Runner stream did not drain after cancellation during a blocked output burst")
	}
}

func uint64String(value uint64) string {
	return strconv.FormatUint(value, 10)
}
