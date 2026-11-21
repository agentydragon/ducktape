package runtimeproxy

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	kubeclient "sigs.k8s.io/controller-runtime/pkg/client"
)

type observedHarnessHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	listCalls    atomic.Int32
	watchCalls   atomic.Int32
	blockWatch   atomic.Bool
	legacyStatus atomic.Bool
	watchStarted chan struct{}
}

func (h *observedHarnessHost) ListSessions(ctx context.Context, _ *harnesshostpb.ListSessionsRequest) (*harnesshostpb.ListSessionsResponse, error) {
	h.listCalls.Add(1)
	if !h.legacyStatus.Load() {
		if err := grpc.SetHeader(ctx, metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
			return nil, err
		}
	}
	return &harnesshostpb.ListSessionsResponse{}, nil
}

func (h *observedHarnessHost) WatchSessions(_ *harnesshostpb.WatchSessionsRequest, stream harnesshostpb.HarnessHost_WatchSessionsServer) error {
	h.watchCalls.Add(1)
	if h.legacyStatus.Load() {
		// The old WatchSessions upsert had this exact wire layout. Its SessionSpec
		// collides with Session.active_turn_id; a reader must reject the header first.
		return stream.SendMsg(&sessionpb.ListSessionsResponse{Sessions: []*sessionpb.SessionSummary{{SessionId: "legacy", Spec: &sessionpb.SessionSpec{Model: "legacy-model"}}}})
	}
	if err := stream.SendHeader(metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return err
	}
	if h.blockWatch.Load() {
		close(h.watchStarted)
		<-stream.Context().Done()
		return stream.Context().Err()
	}
	return stream.Send(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}})
}

func TestRuntimeProxyAuthenticatesCallerAndSandboxIdentity(t *testing.T) {
	f := newSandboxFixture(t)
	server := f.newServer(t)
	destination, err := server.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	credentials, err := f.runtimeTLS.ServerCredentials(peertls.RuntimeProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	runtimeServer := grpc.NewServer(grpc.Creds(credentials))
	runtime := &observedRuntime{
		sandboxUID: string(f.sandbox.UID), incarnationUID: string(f.pod.UID), bootID: "proxy-test-boot",
	}
	host := &observedHarnessHost{watchStarted: make(chan struct{})}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(runtimeServer, runtime)
	harnesshostpb.RegisterHarnessHostServer(runtimeServer, host)
	destination.target = grpcserver.Serve(t, runtimeServer)

	_, observerDone, _ := runObserver(t, server, destination)
	awaitConnection(t, server, string(f.sandbox.UID), "proxy-test-boot", observerDone)

	proxy := serveServer(t, server, server.RegisterProxy)
	client := environmentruntimepb.NewEnvironmentRuntimeClient(proxy)
	if _, err := client.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("authorized app call did not reach Runtime: %v", err)
	}
	if runtime.calls.Load() != 1 {
		t.Fatal("authorized app call did not reach the direct Runtime server")
	}
	hostClient := harnesshostpb.NewHarnessHostClient(proxy)
	if _, err := hostClient.ListSessions(f.callerContext(peertls.AppURI), &harnesshostpb.ListSessionsRequest{}); err != nil {
		t.Fatalf("configured reader HarnessHost ListSessions = %v", err)
	}
	var hostHeader metadata.MD
	if _, err := hostClient.ListSessions(f.callerContext(peertls.SessionsURI), &harnesshostpb.ListSessionsRequest{}, grpc.Header(&hostHeader)); err != nil {
		t.Fatalf("authorized Sessions HarnessHost call did not reach Runtime: %v", err)
	}
	if err := harnesshost.RequireSessionVersion(hostHeader); err != nil {
		t.Fatalf("proxy ListSessions version header: %v", err)
	}
	if host.listCalls.Load() != 2 {
		t.Fatalf("authorized HarnessHost calls reached Runtime %d times, want 2", host.listCalls.Load())
	}
	hostWatch, err := hostClient.WatchSessions(f.callerContext(peertls.SessionsURI), &harnesshostpb.WatchSessionsRequest{})
	if err != nil {
		t.Fatalf("authorized HarnessHost watch did not reach Runtime: %v", err)
	}
	watchHeader, err := hostWatch.Header()
	if err != nil {
		t.Fatal(err)
	}
	if err := harnesshost.RequireSessionVersion(watchHeader); err != nil {
		t.Fatalf("proxy WatchSessions version header: %v", err)
	}
	watchFrame, err := hostWatch.Recv()
	if err != nil || watchFrame.GetCaughtUp() == nil {
		t.Fatalf("HarnessHost watch frame = %v, %v; want caught-up snapshot", watchFrame, err)
	}
	if _, err := hostWatch.Recv(); err == nil {
		t.Fatal("HarnessHost watch stayed open after Runtime ended it")
	}
	if host.watchCalls.Load() != 1 {
		t.Fatalf("authorized HarnessHost watch reached Runtime %d times, want 1", host.watchCalls.Load())
	}
	matchingIncarnationContext := metadata.AppendToOutgoingContext(
		f.callerContext(peertls.AppURI), sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, string(f.pod.UID),
	)
	if _, err := client.GetStatus(matchingIncarnationContext, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("proxy rejected the expected current incarnation: %v", err)
	}
	if runtime.calls.Load() != 2 {
		t.Fatalf("matching incarnation reached Runtime %d times, want 2", runtime.calls.Load())
	}

	wrongHostIncarnationContext := metadata.AppendToOutgoingContext(
		f.callerContext(peertls.SessionsURI), sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, "replaced-runner-uid",
	)
	if _, err := hostClient.ListSessions(wrongHostIncarnationContext, &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("HarnessHost accepted a different expected incarnation: %v", err)
	}
	if got := host.listCalls.Load(); got != 2 {
		t.Fatalf("stale HarnessHost incarnation reached Runtime %d times, want 2", got)
	}

	wrongIncarnationContext := metadata.AppendToOutgoingContext(
		f.callerContext(peertls.AppURI), sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, "replaced-runner-uid",
	)
	if _, err := client.GetStatus(wrongIncarnationContext, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("proxy accepted a different expected incarnation: %v", err)
	}
	wrongSealContext := metadata.AppendToOutgoingContext(
		f.callerContext(peertls.SandboxControllerURI), sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, "replaced-runner-uid",
	)
	if _, err := client.Quiesce(wrongSealContext, &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("controller Quiesce accepted a different incarnation: %v", err)
	}
	if got := runtime.quiesceCalls.Load(); got != 0 {
		t.Fatalf("stale incarnation sent %d Quiesce calls to Runtime", got)
	}
	for _, test := range []struct {
		name   string
		values []string
	}{
		{name: "duplicate", values: []string{string(f.pod.UID), string(f.pod.UID)}},
		{name: "empty", values: []string{""}},
	} {
		t.Run(test.name+" incarnation header", func(t *testing.T) {
			pairs := make([]string, 0, 2*len(test.values))
			for _, value := range test.values {
				pairs = append(pairs, sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, value)
			}
			ctx := metadata.AppendToOutgoingContext(f.callerContext(peertls.AppURI), pairs...)
			if _, err := client.GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("proxy accepted malformed incarnation header: %v", err)
			}
		})
	}
	if runtime.calls.Load() != 2 {
		t.Fatalf("rejected incarnation headers reached Runtime %d times, want 2", runtime.calls.Load())
	}
	if _, err := client.GetStatus(f.callerContext(peertls.CentralSamplerURI), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("caller outside the Runtime proxy allowlist was accepted: %v", err)
	}

	staleContext := metadata.NewOutgoingContext(context.WithValue(t.Context(), serviceConnectionKey{}, peertls.AppURI), metadata.Pairs(
		sandboxapi.RuntimeProxySandboxMetadataKey, testSandbox,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, f.namespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, "replaced-sandbox-uid",
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	if _, err := client.GetStatus(staleContext, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.NotFound {
		t.Fatalf("proxy accepted a stale Sandbox UID: %v", err)
	}

	commands := []struct {
		name string
		call func(context.Context) error
	}{
		{"open", func(ctx context.Context) error {
			_, err := hostClient.CreateSession(ctx, &harnesshostpb.CreateSessionRequest{})
			return err
		}},
		{"input", func(ctx context.Context) error {
			_, err := hostClient.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{})
			return err
		}},
		{"model", func(ctx context.Context) error {
			_, err := hostClient.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{})
			return err
		}},
		{"effort", func(ctx context.Context) error {
			_, err := hostClient.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{})
			return err
		}},
		{"interrupt", func(ctx context.Context) error {
			_, err := hostClient.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{})
			return err
		}},
	}
	// The fixture's command methods return Unimplemented only after forwarding.
	for _, command := range commands {
		if err := command.call(f.callerContext(peertls.SessionsURI)); status.Code(err) != codes.Unimplemented {
			t.Fatalf("%s before deletion = %v, want forwarded", command.name, err)
		}
	}

	// The controller can still seal a live Runtime while a held suspend is visible in the
	// Server's cache. Other proxy methods remain unavailable in that phase.
	f.sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	f.sandbox.Status.StopStage = ""
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus in held suspend = %v, want Unavailable", err)
	}
	if _, err := hostClient.SubmitInput(f.callerContext(peertls.SessionsURI), &harnesshostpb.SubmitInputRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput in held suspend = %v, want Unavailable", err)
	}
	if _, err := client.Quiesce(f.callerContext(peertls.SandboxControllerURI), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("controller Quiesce in held suspend did not reach Runtime: %v", err)
	}
	if got := runtime.quiesceCalls.Load(); got != 1 {
		t.Fatalf("Runtime received %d Quiesce calls, want 1", got)
	}
	if _, err := client.PrepareSuspend(f.callerContext(peertls.SandboxControllerURI), &environmentruntimepb.PrepareSuspendRequest{Generation: 1}); status.Code(err) != codes.Unimplemented {
		t.Fatalf("controller PrepareSuspend in held suspend = %v, want forwarded", err)
	}
	if _, err := client.ResumeAdmission(f.callerContext(peertls.SandboxControllerURI), &environmentruntimepb.ResumeAdmissionRequest{Generation: 1}); status.Code(err) != codes.Unimplemented {
		t.Fatalf("controller ResumeAdmission in held suspend = %v, want forwarded", err)
	}
	if _, err := client.PrepareSuspend(f.callerContext(peertls.AppURI), &environmentruntimepb.PrepareSuspendRequest{Generation: 1}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("app PrepareSuspend = %v, want PermissionDenied", err)
	}
	if _, err := client.Quiesce(f.callerContext(peertls.AppURI), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("app Quiesce in held suspend = %v, want PermissionDenied", err)
	}
	f.sandbox.Status.Incarnation.UID = "replaced-runner-uid"
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Quiesce(f.callerContext(peertls.SandboxControllerURI), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Quiesce for a replaced Runtime = %v, want Unavailable", err)
	}
	f.sandbox.Status.Incarnation.UID = string(f.pod.UID)
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	f.pod.Status.Conditions[0].Status = corev1.ConditionFalse
	if err := f.objects.Status().Update(t.Context(), f.pod); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Quiesce(f.callerContext(peertls.SandboxControllerURI), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Quiesce with an unready Pod = %v, want Unavailable", err)
	}
	if got := runtime.quiesceCalls.Load(); got != 1 {
		t.Fatalf("unready Pod sent a second Quiesce to Runtime: %d", got)
	}

	f.pod.Status.Conditions[0].Status = corev1.ConditionTrue
	if err := f.objects.Status().Update(t.Context(), f.pod); err != nil {
		t.Fatal(err)
	}
	f.sandbox.Status.OperatingMode = sandboxapi.OperatingModeRunning
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	f.sandbox.Finalizers = []string{"test.allegedly.works/hold-deletion"}
	if err := f.objects.Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if err := f.objects.Delete(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if err := f.objects.Get(t.Context(), kubeclient.ObjectKeyFromObject(f.sandbox), f.sandbox); err != nil {
		t.Fatal(err)
	}
	// Observe deletion before Runtime quiescence: no new command may be forwarded,
	// while diagnostics and the controller's sealing handshake remain available.
	for _, command := range commands {
		if err := command.call(f.callerContext(peertls.SessionsURI)); status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("%s during deletion = %v, want FailedPrecondition", command.name, err)
		}
	}
	if _, err := client.GetStatus(f.callerContext(peertls.AppURI), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("deletion diagnostics: %v", err)
	}
	if _, err := client.Quiesce(f.callerContext(peertls.SandboxControllerURI), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("deletion Quiesce: %v", err)
	}

	// A pre-status Runtime can send a valid old protobuf payload whose fields collide
	// with Session. Refuse it before Recv, rather than forwarding a misleading status.
	host.legacyStatus.Store(true)
	if _, err := hostClient.ListSessions(f.callerContext(peertls.SessionsURI), &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("legacy HarnessHost ListSessions = %v, want FailedPrecondition", err)
	}
	legacyWatch, err := hostClient.WatchSessions(f.callerContext(peertls.SessionsURI), &harnesshostpb.WatchSessionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacyWatch.Recv(); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("legacy HarnessHost WatchSessions = %v, want FailedPrecondition before decoding old wire", err)
	}
	host.legacyStatus.Store(false)

	// An invalidated Runtime connection ends an active HarnessHost stream with its retryable
	// connection status instead of leaking the transport's Canceled result.
	host.blockWatch.Store(true)
	fencedWatch, err := hostClient.WatchSessions(f.callerContext(peertls.SessionsURI), &harnesshostpb.WatchSessionsRequest{})
	if err != nil {
		t.Fatalf("start HarnessHost watch for connection invalidation: %v", err)
	}
	select {
	case <-host.watchStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("Runtime did not start the HarnessHost watch")
	}
	connection, err := server.connections.holder(string(f.sandbox.UID))
	if err != nil {
		t.Fatalf("get Runtime connection before invalidation: %v", err)
	}
	server.connections.remove(connection, errSupersededConnection)
	if _, err := fencedWatch.Recv(); status.Code(err) != codes.Aborted {
		t.Fatalf("invalidated HarnessHost watch = %v, want Aborted", err)
	}
}
