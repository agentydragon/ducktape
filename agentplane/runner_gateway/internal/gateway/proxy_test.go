package gateway

import (
	"context"
	"net"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	kubeclient "sigs.k8s.io/controller-runtime/pkg/client"
)

func TestRunnerProxyAuthenticatesCallerAndSandboxIdentity(t *testing.T) {
	f := newSandboxFixture(t)
	gateway := f.newGateway(t, "test-runner-gateway", "192.0.2.30")
	destination, err := gateway.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	runnerListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	credentials, err := f.runnerTLS.ServerCredentials(peertls.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	runnerServer := grpc.NewServer(grpc.Creds(credentials))
	runner := &observedRunner{bootID: "proxy-test-boot"}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(runnerServer, runner)
	go func() { _ = runnerServer.Serve(runnerListener) }()
	t.Cleanup(runnerServer.Stop)
	destination.target = runnerListener.Addr().String()

	observerContext, stopObserver := context.WithCancel(t.Context())
	t.Cleanup(stopObserver)
	observerDone := make(chan error, 1)
	go func() { observerDone <- gateway.observeRunner(observerContext, destination) }()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	for {
		if _, err := gateway.connections.holder(string(f.sandbox.UID)); err == nil {
			break
		}
		select {
		case err := <-observerDone:
			t.Fatalf("Runner observer stopped before connecting: %v", err)
		case <-deadline.C:
			t.Fatal("Runner observer did not become callable")
		case <-poll.C:
		}
	}

	proxyListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxyServer := grpc.NewServer(gateway.Methods().ServerOptions(0)...)
	gateway.RegisterProxy(proxyServer)
	go func() { _ = proxyServer.Serve(proxyListener) }()
	t.Cleanup(proxyServer.Stop)
	proxy, err := grpc.NewClient(proxyListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = proxy.Close() })
	client := environmentruntimepb.NewEnvironmentRuntimeClient(proxy)
	if _, err := client.GetStatus(f.callerContext(f.appToken), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("authorized app call did not reach Runner: %v", err)
	}
	if runner.calls.Load() != 1 {
		t.Fatal("authorized app call did not reach the direct Runner server")
	}
	matchingIncarnationContext := metadata.AppendToOutgoingContext(
		f.callerContext(f.appToken), sandboxapi.RunnerProxyIncarnationUIDMetadataKey, string(f.pod.UID),
	)
	if _, err := client.GetStatus(matchingIncarnationContext, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("proxy rejected the expected current incarnation: %v", err)
	}
	if runner.calls.Load() != 2 {
		t.Fatalf("matching incarnation reached Runner %d times, want 2", runner.calls.Load())
	}

	wrongIncarnationContext := metadata.AppendToOutgoingContext(
		f.callerContext(f.appToken), sandboxapi.RunnerProxyIncarnationUIDMetadataKey, "replaced-runner-uid",
	)
	if _, err := client.GetStatus(wrongIncarnationContext, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("proxy accepted a different expected incarnation: %v", err)
	}
	wrongSealContext := metadata.AppendToOutgoingContext(
		f.callerContext(f.controllerToken), sandboxapi.RunnerProxyIncarnationUIDMetadataKey, "replaced-runner-uid",
	)
	if _, err := client.Quiesce(wrongSealContext, &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("controller Quiesce accepted a different incarnation: %v", err)
	}
	if got := runner.quiesceCalls.Load(); got != 0 {
		t.Fatalf("stale incarnation sent %d Quiesce calls to Runner", got)
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
				pairs = append(pairs, sandboxapi.RunnerProxyIncarnationUIDMetadataKey, value)
			}
			ctx := metadata.AppendToOutgoingContext(f.callerContext(f.appToken), pairs...)
			if _, err := client.GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("proxy accepted malformed incarnation header: %v", err)
			}
		})
	}
	if runner.calls.Load() != 2 {
		t.Fatalf("rejected incarnation headers reached Runner %d times, want 2", runner.calls.Load())
	}
	if _, err := client.GetStatus(f.callerContext(f.executorToken), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("caller outside the Runner proxy allowlist was accepted: %v", err)
	}

	staleContext := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Bearer "+f.appToken,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, "replaced-sandbox-uid",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	if _, err := client.GetStatus(staleContext, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.NotFound {
		t.Fatalf("proxy accepted a stale Sandbox UID: %v", err)
	}

	commands := []struct {
		name string
		call func(context.Context) error
	}{
		{"open", func(ctx context.Context) error {
			_, err := client.CreateSession(ctx, &environmentruntimepb.CreateSessionRequest{})
			return err
		}},
		{"input", func(ctx context.Context) error {
			_, err := client.SubmitInput(ctx, &environmentruntimepb.SubmitInputRequest{})
			return err
		}},
		{"model", func(ctx context.Context) error {
			_, err := client.ChangeModel(ctx, &environmentruntimepb.ChangeModelRequest{})
			return err
		}},
		{"effort", func(ctx context.Context) error {
			_, err := client.ChangeReasoningEffort(ctx, &environmentruntimepb.ChangeReasoningEffortRequest{})
			return err
		}},
		{"interrupt", func(ctx context.Context) error {
			_, err := client.InterruptTurn(ctx, &environmentruntimepb.InterruptTurnRequest{})
			return err
		}},
	}
	// The fixture's command methods return Unimplemented only after forwarding.
	for _, command := range commands {
		if err := command.call(f.callerContext(f.dispatcherToken)); status.Code(err) != codes.Unimplemented {
			t.Fatalf("%s before deletion = %v, want forwarded", command.name, err)
		}
	}

	// The controller can still seal a live Runner while a held suspend is visible in the
	// Gateway's cache. Other proxy methods remain unavailable in that phase.
	f.sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	f.sandbox.Status.StopStage = ""
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetStatus(f.callerContext(f.appToken), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus in held suspend = %v, want Unavailable", err)
	}
	if _, err := client.SubmitInput(f.callerContext(f.dispatcherToken), &environmentruntimepb.SubmitInputRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput in held suspend = %v, want Unavailable", err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("controller Quiesce in held suspend did not reach Runner: %v", err)
	}
	if got := runner.quiesceCalls.Load(); got != 1 {
		t.Fatalf("Runner received %d Quiesce calls, want 1", got)
	}
	if _, err := client.PrepareSuspend(f.callerContext(f.controllerToken), &environmentruntimepb.PrepareSuspendRequest{Generation: 1}); status.Code(err) != codes.Unimplemented {
		t.Fatalf("controller PrepareSuspend in held suspend = %v, want forwarded", err)
	}
	if _, err := client.ResumeAdmission(f.callerContext(f.controllerToken), &environmentruntimepb.ResumeAdmissionRequest{Generation: 1}); status.Code(err) != codes.Unimplemented {
		t.Fatalf("controller ResumeAdmission in held suspend = %v, want forwarded", err)
	}
	if _, err := client.PrepareSuspend(f.callerContext(f.appToken), &environmentruntimepb.PrepareSuspendRequest{Generation: 1}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("app PrepareSuspend = %v, want PermissionDenied", err)
	}
	if _, err := client.Quiesce(f.callerContext(f.appToken), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("app Quiesce in held suspend = %v, want PermissionDenied", err)
	}
	f.sandbox.Status.Incarnation.UID = "replaced-runner-uid"
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Quiesce for a replaced Runner = %v, want Unavailable", err)
	}
	f.sandbox.Status.Incarnation.UID = string(f.pod.UID)
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	f.pod.Status.Conditions[0].Status = corev1.ConditionFalse
	if err := f.objects.Status().Update(t.Context(), f.pod); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &sessionpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Quiesce with an unready Pod = %v, want Unavailable", err)
	}
	if got := runner.quiesceCalls.Load(); got != 1 {
		t.Fatalf("unready Pod sent a second Quiesce to Runner: %d", got)
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
		if err := command.call(f.callerContext(f.dispatcherToken)); status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("%s during deletion = %v, want FailedPrecondition", command.name, err)
		}
	}
	if _, err := client.GetStatus(f.callerContext(f.appToken), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("deletion diagnostics: %v", err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("deletion Quiesce: %v", err)
	}

	stopObserver()
	select {
	case <-observerDone:
	case <-time.After(time.Second):
		t.Fatal("Runner observer did not stop")
	}
}
