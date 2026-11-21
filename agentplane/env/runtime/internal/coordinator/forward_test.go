package coordinator

import (
	"context"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

func TestForwardRuntimeServesHealthAndFencesStateAndDirectCalls(t *testing.T) {
	for _, mode := range []environmentruntimetransport.SecurityMode{environmentruntimetransport.MutualTLS, environmentruntimetransport.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRuntimeHealthAndFencing(t, mode)
		})
	}
}

func testRuntimeHealthAndFencing(t *testing.T, mode environmentruntimetransport.SecurityMode) {
	t.Helper()
	_, peers := startControl(t, lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false"), mode)
	conn := peers.dial(t, environmentruntimetransport.RuntimeProxyURI)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	health, err := grpc_health_v1.NewHealthClient(conn).Check(ctx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil || health.GetStatus() != grpc_health_v1.HealthCheckResponse_SERVING {
		t.Fatalf("pre-watch health = %v, error %v; want SERVING", health, err)
	}

	peers.bind(t, "sandbox-uid", "pod-uid-1")
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if first.GetEnvironmentRuntimeBootId() == "" || first.GetSandboxUid() != "sandbox-uid" || first.GetIncarnationUid() != "pod-uid-1" || first.GetFacts() == nil {
		t.Fatalf("first WatchState response has invalid identity or facts: %v", first)
	}

	callContext := func(incarnation, bootID string) context.Context {
		return fencedContext(ctx, "sandbox-uid", incarnation, bootID)
	}
	statusClient := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	hostClient := harnesshostpb.NewHarnessHostClient(conn)
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("current GetStatus = %v", err)
	}
	for _, stale := range []context.Context{callContext("old-pod", first.GetEnvironmentRuntimeBootId()), callContext("pod-uid-1", "old-boot")} {
		if _, err := statusClient.GetStatus(stale, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
			t.Fatalf("stale GetStatus = %v", err)
		}
	}
	if _, err := statusClient.GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("GetStatus without fencing metadata = %v, want Unauthenticated", err)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", "old-runner-boot"), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus for old Environment Runtime boot = %v, want Unavailable", err)
	}
	if _, err := hostClient.SubmitInput(callContext("pod-uid-1", "old-runner-boot"), &harnesshostpb.SubmitInputRequest{
		SessionId: "not-created", CommandId: "stale-boot-input", Text: "must not be admitted",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput for old Environment Runtime boot = %v, want retryable rejection before admission", err)
	}
	currentStatus, err := statusClient.GetStatus(callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{})
	if err != nil {
		t.Fatalf("current-epoch GetStatus: %v", err)
	}
	if currentStatus.GetEnvironmentRuntimeBootId() != first.GetEnvironmentRuntimeBootId() {
		t.Fatalf("GetStatus boot ID = %q, WatchState boot ID = %q", currentStatus.GetEnvironmentRuntimeBootId(), first.GetEnvironmentRuntimeBootId())
	}
	newWatch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	second, err := newWatch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if second.GetIncarnationUid() != "pod-uid-1" || second.GetEnvironmentRuntimeBootId() != first.GetEnvironmentRuntimeBootId() {
		t.Fatalf("successor WatchState response = %v", second)
	}
	if _, err := statusClient.GetStatus(callContext("pod-uid-1", second.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("independent watcher changed call authorization: %v", err)
	}
	staleExecution, err := executorpb.NewExecutorClient(conn).OpenSession(
		callContext("pod-uid-1", first.GetEnvironmentRuntimeBootId()),
		&executorpb.OpenSessionRequest{
			Scope: &executorpb.ExecutionScope{
				Destination: &identitypb.SandboxDestination{
					Target: &identitypb.EnvironmentTarget{Namespace: "test", Name: "sandbox", Uid: "sandbox-uid"}, Owner: &identitypb.ServiceAccount{Namespace: "test", Name: "sandbox"},
				},
				ExpectedIncarnationUid: "pod-uid-2", SessionId: "sampler-session",
			},
			RequestId: "stale-open-session",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := staleExecution.Recv(); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("OpenSession for a stale incarnation = %v, want FailedPrecondition", err)
	}
}

func TestRuntimeRejectsControlCertificateForAnotherSandbox(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	wrongURI, err := workloadid.ID{Namespace: "another-namespace", Name: "another-sandbox", CredentialID: "credential-id"}.URI()
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
		t.Fatal("Environment Runtime accepted a control certificate for another Sandbox")
	}
}

func TestForwardRuntimeRetriesSameAdmissionAfterLostReplyAndWatchCancellation(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	_, peers := startControl(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"), environmentruntimetransport.MutualTLS)
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
		if method == harnesshostpb.HarnessHost_SubmitInput_FullMethodName && err == nil && dropFirstReply {
			dropFirstReply = false
			firstCursor = response.(*harnesshostpb.SubmitInputResponse).GetAdmission().GetCursor()
			cancelFirst()
			return status.Error(codes.Unavailable, "simulated lost Session admission reply")
		}
		return err
	}
	conn := peers.dial(t, environmentruntimetransport.RuntimeProxyURI, grpc.WithUnaryInterceptor(unaryInterceptor))
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	hostClient := harnesshostpb.NewHarnessHostClient(conn)
	watchCtx, cancelWatch := context.WithCancel(ctx)
	peers.bind(t, "sandbox-uid", "pod-uid")
	watch, err := client.WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid",
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	callContext := func(parent context.Context, bootID string) context.Context {
		return fencedContext(parent, "sandbox-uid", "pod-uid", bootID)
	}
	if _, err := hostClient.CreateSession(callContext(ctx, first.GetEnvironmentRuntimeBootId()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	submitCtx, cancelSubmit := context.WithCancel(ctx)
	cancelFirst = cancelSubmit
	if _, err := hostClient.SubmitInput(callContext(submitCtx, first.GetEnvironmentRuntimeBootId()), &harnesshostpb.SubmitInputRequest{
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
	var watchErr error
	for {
		_, watchErr = watch.Recv()
		if watchErr != nil {
			break
		}
	}
	if status.Code(watchErr) != codes.Canceled {
		t.Fatalf("Gateway watch cancellation = %v, want Canceled", watchErr)
	}
	if _, err := client.GetStatus(callContext(ctx, first.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("GetStatus after canceled WatchState: %v", err)
	}
	retry, err := hostClient.SubmitInput(callContext(ctx, first.GetEnvironmentRuntimeBootId()), &harnesshostpb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "same-command", Text: "hello",
	})
	if err != nil {
		t.Fatalf("retry with the admitted command ID after observer loss: %v", err)
	}
	if retry.GetAdmission().GetCursor() != firstCursor {
		t.Fatalf("retry cursor=%d, want original %d", retry.GetAdmission().GetCursor(), firstCursor)
	}
	next, err := hostClient.SubmitInput(callContext(ctx, first.GetEnvironmentRuntimeBootId()), &harnesshostpb.SubmitInputRequest{
		SessionId: "session-1", CommandId: "next-command", Text: "world",
	})
	if err != nil {
		t.Fatalf("next input after observer loss: %v", err)
	}
	if next.GetAdmission().GetCursor() <= firstCursor {
		t.Fatalf("next input cursor=%d, want greater than retained cursor %d", next.GetAdmission().GetCursor(), firstCursor)
	}
}

func TestForwardRuntimeRejectsUnconfiguredExecutionSession(t *testing.T) {
	_, peers := startControl(t, lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false"), environmentruntimetransport.MutualTLS)
	conn := peers.dial(t, environmentruntimetransport.RuntimeProxyURI)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	peers.bind(t, "sandbox-uid", "pod-uid")
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid",
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		t.Fatal(err)
	}
	callContext := fencedContext(ctx, "sandbox-uid", "pod-uid", first.GetEnvironmentRuntimeBootId())
	executorClient := executorpb.NewExecutorClient(conn)
	refused, err := executorClient.OpenSession(callContext, &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: "test", Name: "sandbox", Uid: "sandbox-uid"}, Owner: &identitypb.ServiceAccount{Namespace: "test", Name: "sandbox"},
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

type burstExecutor struct {
	executorpb.UnimplementedExecutorServer
	sent chan error
}

func (s *burstExecutor) OpenSession(_ *executorpb.OpenSessionRequest, stream executorpb.Executor_OpenSessionServer) error {
	if err := stream.Send(&executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED}, ExecutionSessionId: "native-session"}); err != nil {
		return err
	}
	for i := range 12 {
		payload := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"index":%d,"data":%q}}`, i, strings.Repeat("x", 600_000)))
		if err := stream.Send(&executorpb.OpenSessionResponse{NativeFrames: []*executorpb.ExecutionNativeFrame{{Direction: "notification", Payload: payload}}}); err != nil {
			select {
			case s.sent <- err:
			default:
			}
			return err
		}
	}
	s.sent <- nil
	<-stream.Context().Done()
	return nil
}

func TestForwardRuntimeBackpressuresExecutorOutputBurst(t *testing.T) {
	const burst = 12
	const notificationData = 600_000
	backend := &burstExecutor{sent: make(chan error, 1)}
	address := startExecutorTestGRPC(t, backend)
	r := newExecutionRuntime(t, address, nil)
	client := startDirectExecutionTestClient(t, r, "pod-uid")
	stream, err := client.executor.OpenSession(client.callCtx, &executorpb.OpenSessionRequest{Scope: client.scope("session-1"), RequestId: "open-notification-session"})
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
	case err := <-backend.sent:
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

func TestControllerBindsRuntimeForIndependentAuthenticatedProxyReplicas(t *testing.T) {
	_, peers := startControl(t, lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false"), environmentruntimetransport.MutualTLS)
	controllerConn := peers.dial(t, environmentruntimetransport.SandboxControllerURI)
	controller := environmentruntimepb.NewEnvironmentRuntimeClient(controllerConn)
	gateway := environmentruntimepb.NewEnvironmentRuntimeClient(peers.dial(t, environmentruntimetransport.RuntimeProxyURI))
	gatewayB := environmentruntimepb.NewEnvironmentRuntimeClient(peers.dial(t, environmentruntimetransport.RuntimeProxyURI))
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	passiveCtx, cancelPassive := context.WithCancel(ctx)
	defer cancelPassive()
	passiveRequest := &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", ObserveOnly: true,
	}
	// Gateway replicas cannot choose the workload UID before the Controller binds it.
	for _, client := range []environmentruntimepb.EnvironmentRuntimeClient{gateway, gatewayB} {
		stream, err := client.WatchState(ctx, &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid"})
		if err == nil {
			_, err = stream.Recv()
		}
		if status.Code(err) != codes.Unavailable {
			t.Fatalf("Gateway established incarnation before Controller: %v", err)
		}
	}
	passive, err := controller.WatchState(passiveCtx, passiveRequest)
	if err != nil {
		t.Fatal(err)
	}
	initial, err := passive.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if initial.GetFacts() == nil || initial.GetEnvironmentRuntimeBootId() == "" || initial.GetIncarnationUid() != "pod-uid" {
		t.Fatalf("passive snapshot = %v", initial)
	}
	gatewayWatchCtx, cancelGatewayWatch := context.WithCancel(ctx)
	defer cancelGatewayWatch()
	watchA, err := gateway.WatchState(gatewayWatchCtx, &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid"})
	if err != nil {
		t.Fatal(err)
	}
	snapshotA, err := watchA.Recv()
	if err != nil {
		t.Fatal(err)
	}
	watchB, err := gatewayB.WatchState(ctx, &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid"})
	if err != nil {
		t.Fatal(err)
	}
	snapshotB, err := watchB.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if snapshotA.GetEnvironmentRuntimeBootId() != initial.GetEnvironmentRuntimeBootId() || snapshotB.GetEnvironmentRuntimeBootId() != initial.GetEnvironmentRuntimeBootId() {
		t.Fatalf("independent Gateway snapshots: %v, %v", snapshotA, snapshotB)
	}
	call := func(uid, incarnation, boot string) context.Context { return fencedContext(ctx, uid, incarnation, boot) }
	checkReplicas := func() {
		t.Helper()
		for _, client := range []environmentruntimepb.EnvironmentRuntimeClient{gateway, gatewayB} {
			if _, err := client.GetStatus(call("sandbox-uid", "pod-uid", initial.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{}); err != nil {
				t.Fatalf("current replica call: %v", err)
			}
			for _, stale := range []context.Context{call("sandbox-uid", "old-pod", initial.GetEnvironmentRuntimeBootId()), call("sandbox-uid", "pod-uid", "old-boot")} {
				if _, err := client.GetStatus(stale, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
					t.Fatalf("stale replica call admitted: %v", err)
				}
			}
			if _, err := client.GetStatus(call("old-sandbox", "pod-uid", initial.GetEnvironmentRuntimeBootId()), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.PermissionDenied {
				t.Fatalf("foreign Sandbox call admitted: %v", err)
			}
		}
	}
	checkReplicas()
	cancelGatewayWatch()
	if _, err := watchA.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("Gateway A watch cancellation: %v", err)
	}
	checkReplicas()
	callCtx := call("sandbox-uid", "pod-uid", initial.GetEnvironmentRuntimeBootId())
	for _, test := range []struct {
		name    string
		client  environmentruntimepb.EnvironmentRuntimeClient
		request *environmentruntimepb.WatchStateRequest
		code    codes.Code
	}{
		{name: "Gateway cannot observe passively", client: gateway, request: passiveRequest, code: codes.Unauthenticated},
		{name: "Controller cannot open Gateway watch", client: controller, request: &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid"}, code: codes.Unauthenticated},
		{name: "foreign Sandbox", client: controller, request: &environmentruntimepb.WatchStateRequest{SandboxUid: "foreign", IncarnationUid: "pod-uid", ObserveOnly: true}, code: codes.PermissionDenied},
		{name: "missing Sandbox", client: controller, request: &environmentruntimepb.WatchStateRequest{IncarnationUid: "pod-uid", ObserveOnly: true}, code: codes.InvalidArgument},
		{name: "missing incarnation", client: controller, request: &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", ObserveOnly: true}, code: codes.InvalidArgument},
		{name: "Gateway cannot adopt old UID", client: gateway, request: &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "old-pod"}, code: codes.FailedPrecondition},
		{name: "passive cannot replace incarnation", client: controller, request: &environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox-uid", IncarnationUid: "replacement", ObserveOnly: true}, code: codes.FailedPrecondition},
	} {
		t.Run(test.name, func(t *testing.T) {
			stream, err := test.client.WatchState(ctx, test.request)
			if err == nil {
				_, err = stream.Recv()
			}
			if status.Code(err) != test.code {
				t.Fatalf("WatchState error = %v, want %s", err, test.code)
			}
		})
	}
	proxy := environmentruntimepb.NewEnvironmentRuntimeClient(peers.dial(t, environmentruntimetransport.RuntimeProxyURI))
	if _, err := proxy.GetStatus(callCtx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("role-specific Runtime proxy status: %v", err)
	}
	proxyWatch, err := proxy.WatchState(ctx, passiveRequest)
	if err == nil {
		_, err = proxyWatch.Recv()
	}
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Runtime proxy must not inherit Controller passive watch: %v", err)
	}
	legacy := environmentruntimepb.NewEnvironmentRuntimeClient(peers.dial(t, environmentruntimetransport.GatewayURI))
	if _, err := legacy.GetStatus(callCtx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("retired Gateway peer bypassed the Runtime TLS allowlist: %v", err)
	}
	unlisted := environmentruntimepb.NewEnvironmentRuntimeClient(peers.dial(t, environmentruntimetransport.SessionsURI))
	if _, err := unlisted.GetStatus(callCtx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Sessions bypassed the Runtime proxy TLS allowlist: %v", err)
	}
	if _, err := controller.GetStatus(callCtx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Controller GetStatus with valid Gateway metadata = %v", err)
	}
	if _, err := executorpb.NewExecutorClient(controllerConn).StartProcess(callCtx, &executorpb.StartProcessRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Controller Executor call with valid Gateway metadata = %v", err)
	}
	// Controller observation may overlap Gateway observation without owning its lifetime.
	second, err := controller.WatchState(ctx, passiveRequest)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot, err := second.Recv(); err != nil || snapshot.GetEnvironmentRuntimeBootId() != initial.GetEnvironmentRuntimeBootId() {
		t.Fatalf("concurrent passive snapshot = %v, error = %v", snapshot, err)
	}
	cancelPassive()
	if _, err := passive.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("canceled passive watch = %v", err)
	}
	checkReplicas()
	if _, err := gateway.GetStatus(callCtx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("Gateway call after passive cancellation: %v", err)
	}
}

func TestPassiveWatchAdoptsReplacementAfterRestoringSealedState(t *testing.T) {
	config := lifecycleConfig(t, "http://127.0.0.1", t.TempDir(), nil, "/bin/false")
	_, retained, closeBacking := openPersistentEnvironmentState(t, config.StateDir)
	if err := retained.AdoptIncarnation("old-pod"); err != nil {
		t.Fatal(err)
	}
	if err := retained.EnterLameduck(); err != nil {
		t.Fatal(err)
	}
	if err := closeBacking(); err != nil {
		t.Fatal(err)
	}
	config.HistoryIngress.Target = startAcknowledgingHistory(t)
	// Start a fresh Runtime without the fixture's ordinary initial Gateway adoption.
	// It must restore the seal and let the Controller name the replacement Pod.
	r, err := newTestRuntime(t, config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer cancel()
		if err := r.Close(ctx); err != nil {
			t.Errorf("close restored Runtime: %v", err)
		}
	})
	if err := r.startSessions(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	if err := r.waitForInitialSessions(ctx); err != nil {
		t.Fatal(err)
	}
	if uid, found, err := r.state.SealedUID(); err != nil || !found || uid != "old-pod" {
		t.Fatalf("restored seal = %q, found=%t, error=%v", uid, found, err)
	}
	// The fixture explicitly uses insecure transport at both endpoints.
	conn := dialControl(t, serveControl(t, r), r.security, "")
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: "sandbox-uid", IncarnationUid: "new-pod", ObserveOnly: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if response, err := watch.Recv(); err != nil || response.GetIncarnationUid() != "new-pod" || response.GetFacts() == nil {
		t.Fatalf("replacement passive observation = %v, error=%v", response, err)
	}
	if uid, known := r.state.IncarnationUID(); !known || uid != "new-pod" {
		t.Fatalf("Runtime incarnation = %q, known=%t", uid, known)
	}
	if uid, found, err := r.state.SealedUID(); err != nil || found {
		t.Fatalf("replacement retained prior seal = %q, found=%t, error=%v", uid, found, err)
	}
}

// Bind through the same distinct Controller operation used by production. The
// binding outlives this observation; Gateway tests cannot install it themselves.
func bindTestIncarnation(t *testing.T, client environmentruntimepb.EnvironmentRuntimeClient, sandboxUID, incarnation string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	stream, err := client.WatchState(ctx, &environmentruntimepb.WatchStateRequest{SandboxUid: sandboxUID, IncarnationUid: incarnation, ObserveOnly: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("Controller initial binding: %v", err)
	}
}
