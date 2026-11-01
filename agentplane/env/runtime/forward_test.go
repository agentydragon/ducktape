package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestForwardRunnerServesHealthAndFencesStateAndDirectCalls(t *testing.T) {
	for _, mode := range []environmentruntimetransport.SecurityMode{environmentruntimetransport.MutualTLS, environmentruntimetransport.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRunnerHealthAndFencing(t, mode)
		})
	}
}

func testRunnerHealthAndFencing(t *testing.T, mode environmentruntimetransport.SecurityMode) {
	t.Helper()
	runnerSecurity := environmentruntimetransport.Security{Mode: mode}
	gatewaySecurity := environmentruntimetransport.Security{Mode: mode}
	var runnerURI string
	if mode == environmentruntimetransport.MutualTLS {
		ca := environmentruntimetransporttesting.NewCA(t)
		var err error
		runnerURI, err = environmentruntimetransport.EnvironmentRuntimeURI("test", "sandbox-uid", "credential-id")
		if err != nil {
			t.Fatal(err)
		}
		runnerTLS := ca.Issue(t, runnerURI)
		gatewayTLS := ca.Issue(t, environmentruntimetransport.GatewayURI)
		runnerSecurity.TLS, gatewaySecurity.TLS = &runnerTLS, &gatewayTLS
	}
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	config.Control = configuration.EnvironmentRuntimeControlConfiguration{ListenAddress: "127.0.0.1:0", Security: runnerSecurity}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorProxy)
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

	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1", ConnectionEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if first.GetEnvironmentRuntimeBootId() == "" || first.GetSandboxUid() != "sandbox-uid" || first.GetIncarnationUid() != "pod-uid-1" || first.GetConnectionEpoch() != 1 || first.GetFacts() == nil {
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
	statusClient := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	if _, err := statusClient.GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("GetStatus without fencing metadata = %v, want Unauthenticated", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", "old-runner-boot", 1), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus for old runner boot = %v, want Unavailable", err)
	}
	if _, err := statusClient.SubmitInput(callContext("pod-uid-1", "old-runner-boot", 1), &environmentruntimepb.SubmitInputRequest{
		SessionId: "not-created", CommandId: "stale-boot-input", Text: "must not be admitted",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput for old runner boot = %v, want retryable rejection before admission", err)
	}
	currentStatus, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId(), 1), &sessionpb.GetStatusRequest{})
	if err != nil {
		t.Fatalf("current-epoch GetStatus: %v", err)
	}
	if currentStatus.GetEnvironmentRuntimeBootId() != first.GetEnvironmentRuntimeBootId() {
		t.Fatalf("GetStatus boot ID = %q, WatchState boot ID = %q", currentStatus.GetEnvironmentRuntimeBootId(), first.GetEnvironmentRuntimeBootId())
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId(), 2), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("stale GetStatus error = %v, want Unavailable", err)
	}

	newWatch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
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
	if _, err := statusClient.GetStatus(callContext("pod-uid-2", second.GetEnvironmentRuntimeBootId(), 2), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("successor watcher was deactivated by its predecessor: %v", err)
	}
	staleExecution, err := executorpb.NewExecutorClient(conn).OpenSession(
		callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId(), 1),
		&executorpb.OpenSessionRequest{
			Scope: &executorpb.ExecutionScope{
				Destination: &sandboxespb.SandboxDestination{
					Sandbox: "sandbox", SandboxUid: "sandbox-uid",
					Owner: &sandboxespb.ServiceAccount{Namespace: "test", Name: "sandbox"},
				},
				ExpectedIncarnationUid: "pod-uid-2", SessionId: "sampler-session",
			},
			RequestId: "stale-open-session",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := staleExecution.Recv(); status.Code(err) != codes.Unavailable {
		t.Fatalf("OpenSession on a superseded epoch = %v, want Unavailable", err)
	}
}

func TestRunnerRejectsControlCertificateForAnotherSandbox(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	wrongURI, err := environmentruntimetransport.EnvironmentRuntimeURI("another-namespace", "another-sandbox", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	wrongTLS := ca.Issue(t, wrongURI)
	config := Config{
		StateDir: t.TempDir(),
		Launch:   configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}},
		Control: configuration.EnvironmentRuntimeControlConfiguration{
			ListenAddress: "127.0.0.1:8090", Security: environmentruntimetransport.Security{Mode: environmentruntimetransport.MutualTLS, TLS: &wrongTLS},
		},
	}
	if _, err := New(config); err == nil {
		t.Fatal("Runner accepted a control certificate for another Sandbox")
	}
}

func TestForwardRunnerRetriesSameAdmissionAfterLostReplyAndWatchCancellation(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environmentruntimetransport.EnvironmentRuntimeURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, environmentruntimetransport.GatewayURI)
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.Control = configuration.EnvironmentRuntimeControlConfiguration{ListenAddress: "127.0.0.1:0", Security: environmentruntimetransport.Security{Mode: environmentruntimetransport.MutualTLS, TLS: &runnerTLS}}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorProxy)
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
	var firstCursor uint64
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
		if method == "/agentplane.env.runtime.control.EnvironmentRuntime/SubmitInput" && err == nil && dropFirstReply {
			dropFirstReply = false
			firstCursor = response.(*environmentruntimepb.SubmitInputResponse).GetAdmission().GetCursor()
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
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	watchCtx, cancelWatch := context.WithCancel(ctx)
	watch, err := client.WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
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
	if _, err := client.OpenSession(callContext(ctx, 1, first.GetEnvironmentRuntimeBootId()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	submitCtx, cancelSubmit := context.WithCancel(ctx)
	cancelFirst = cancelSubmit
	if _, err := client.SubmitInput(callContext(submitCtx, 1, first.GetEnvironmentRuntimeBootId()), &environmentruntimepb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "same-command", Text: "hello",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("first direct SubmitInput error = %v, want the simulated lost reply", err)
	}
	if submitCtx.Err() != context.Canceled {
		t.Fatalf("first SubmitInput context error = %v, want cancellation after admission", submitCtx.Err())
	}
	cancelSubmit()
	if firstCursor == 0 {
		t.Fatal("lost reply did not carry a durable admission cursor")
	}
	cancelWatch()
	deadline := time.Now().Add(time.Second)
	for {
		_, err := client.GetStatus(callContext(ctx, 1, first.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{})
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
	watch, err = client.WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ConnectionEpoch: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	retry, err := client.SubmitInput(callContext(ctx, 2, second.GetEnvironmentRuntimeBootId()), &environmentruntimepb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "same-command", Text: "hello",
	})
	if err != nil {
		t.Fatalf("retry with the admitted command ID: %v", err)
	}
	if retry.GetAdmission().GetCursor() != firstCursor {
		t.Fatalf("retry cursor=%d, want original %d", retry.GetAdmission().GetCursor(), firstCursor)
	}
}

func TestForwardRunnerRejectsUnconfiguredExecutionSession(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environmentruntimetransport.EnvironmentRuntimeURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, environmentruntimetransport.GatewayURI)
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	config.Control = configuration.EnvironmentRuntimeControlConfiguration{ListenAddress: "127.0.0.1:0", Security: environmentruntimetransport.Security{Mode: environmentruntimetransport.MutualTLS, TLS: &runnerTLS}}
	r := newLifecycleRunner(t, config)
	listener, server, err := r.control.Listen(r.executorProxy)
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
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
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
		control.BootMetadata, first.GetEnvironmentRuntimeBootId(),
		control.EpochMetadata, "1",
	)
	executorClient := executorpb.NewExecutorClient(conn)
	refused, err := executorClient.OpenSession(callContext, &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &sandboxespb.SandboxDestination{
				Sandbox: "sandbox", SandboxUid: "sandbox-uid",
				Owner: &sandboxespb.ServiceAccount{Namespace: "test", Name: "sandbox"},
			},
			ExpectedIncarnationUid: "pod-uid", SessionId: "session-1",
		},
		RequestId: "open-unconfigured-session",
	})
	if err != nil {
		t.Fatal(err)
	}
	response, err := refused.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_REJECTED {
		t.Fatalf("unconfigured execution session response = %v, %v; want rejected outcome", response, err)
	}
	if _, err := refused.Recv(); err != io.EOF {
		t.Fatalf("rejected execution session stream ended with %v, want EOF", err)
	}
}

func TestForwardRunnerBackpressuresExecutorOutputBurst(t *testing.T) {
	const burst = 12
	const notificationData = 600_000
	sent := make(chan error, 1)
	execServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			select {
			case sent <- err:
			default:
			}
			return
		}
		defer connection.CloseNow()
		_, payload, err := connection.Read(request.Context())
		if err != nil {
			select {
			case sent <- err:
			default:
			}
			return
		}
		var initialize struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(payload, &initialize); err != nil {
			select {
			case sent <- err:
			default:
			}
			return
		}
		response := []byte(fmt.Sprintf(`{"id":%q,"result":{"sessionId":"native-session","environmentInfo":{"shell":{"name":"bash","path":"/bin/bash"}}}}`, initialize.ID))
		if err := connection.Write(request.Context(), websocket.MessageText, response); err != nil {
			select {
			case sent <- err:
			default:
			}
			return
		}
		if _, _, err := connection.Read(request.Context()); err != nil { // initialized notification
			select {
			case sent <- err:
			default:
			}
			return
		}
		for i := range burst {
			payload := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"index":%d,"data":%q}}`, i, strings.Repeat("x", notificationData)))
			if err := connection.Write(request.Context(), websocket.MessageText, payload); err != nil {
				select {
				case sent <- err:
				default:
				}
				return
			}
		}
		sent <- nil
		<-request.Context().Done()
	}))
	defer execServer.Close()
	r := newExecutionRuntime(t, "ws"+strings.TrimPrefix(execServer.URL, "http"), nil)
	client := startDirectExecutionTestClient(t, r, "pod-uid")
	stream, err := client.executor.OpenSession(client.callCtx, &executorpb.OpenSessionRequest{
		Scope: client.scope("session-1"), RequestId: "open-notification-session",
	})
	if err != nil {
		t.Fatal(err)
	}
	opened, err := stream.Recv()
	if err != nil || opened.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("execution session readiness = %v, %v", opened, err)
	}
	var transferred int
	for i := range burst {
		response, err := stream.Recv()
		if err != nil {
			t.Fatalf("receive notification %d: %v", i, err)
		}
		if len(response.GetNativeFrames()) != 1 {
			t.Fatalf("notification response %d = %v", i, response)
		}
		frame := response.GetNativeFrames()[0]
		want := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"index":%d,"data":%q}}`, i, strings.Repeat("x", notificationData)))
		if frame.GetDirection() != "notification" || string(frame.GetPayload()) != string(want) {
			t.Fatalf("notification %d changed: direction=%q payload bytes=%d", i, frame.GetDirection(), len(frame.GetPayload()))
		}
		transferred += len(frame.GetPayload())
	}
	select {
	case err := <-sent:
		if err != nil {
			t.Fatalf("executor produced notifications: %v", err)
		}
	case <-client.ctx.Done():
		t.Fatal("executor notification stream did not finish")
	}
	const maxNativeFrameBytes = 1 << 20
	if transferred <= 2*maxNativeFrameBytes {
		t.Fatalf("native evidence transferred %d bytes, want more than %d", transferred, 2*maxNativeFrameBytes)
	}
}

func uint64String(value uint64) string {
	return strconv.FormatUint(value, 10)
}
