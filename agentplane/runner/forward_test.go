package runner

import (
	"context"
	"io"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runner_execpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	runnertransporttesting "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestForwardRunnerServesHealthAndFencesStateAndDirectCalls(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runnertransport.RunnerURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, runnertransport.GatewayURI)
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	config.Control = RunnerControlConfiguration{
		ListenAddress: "127.0.0.1:0", TLS: runnerTLS,
	}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.forwardServer()
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
			controlSandboxMetadata, "sandbox-uid",
			controlIncarnationMetadata, incarnation,
			controlBootMetadata, bootID,
			controlEpochMetadata, uint64String(epoch),
		)
	}
	statusClient := runnerservicepb.NewRunnerClient(conn)
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetRunnerBootId(), 1), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatalf("current-epoch GetStatus: %v", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetRunnerBootId(), 2), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("stale GetStatus error = %v, want Aborted", err)
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
	config := Config{
		StateDir: t.TempDir(),
		Launch:   SandboxLaunch{Sandbox: SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}},
		Control: RunnerControlConfiguration{
			ListenAddress: "127.0.0.1:8090", TLS: ca.Issue(t, wrongURI),
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
	config.Control = RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", TLS: runnerTLS}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.forwardServer()
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
			controlSandboxMetadata, "sandbox-uid",
			controlIncarnationMetadata, "pod-uid",
			controlBootMetadata, bootID,
			controlEpochMetadata, uint64String(epoch),
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
		r.directMu.RLock()
		current := r.directWatch
		inactive := current == nil || current.ctx.Err() != nil
		r.directMu.RUnlock()
		if inactive {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("canceled WatchState remained active")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := client.GetStatus(callContext(ctx, 1, first.GetRunnerBootId()), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("call after WatchState cancellation = %v, want Aborted", err)
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
	config.Control = RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", TLS: runnerTLS}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.forwardServer()
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
		controlSandboxMetadata, "sandbox-uid",
		controlIncarnationMetadata, "pod-uid",
		controlBootMetadata, first.GetRunnerBootId(),
		controlEpochMetadata, "1",
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

	closeStreams := make([]func() error, 0, maxExecutorStreamsPerRunner)
	defer func() {
		for _, closeStream := range closeStreams {
			_ = closeStream()
		}
	}()
	for i := 0; i < maxExecutorStreamsPerRunner; i++ {
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

func uint64String(value uint64) string {
	return strconv.FormatUint(value, 10)
}
