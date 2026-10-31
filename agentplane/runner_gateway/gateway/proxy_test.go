package gateway

import (
	"context"
	"net"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
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
	credentials, err := f.runnerTLS.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	runnerServer := grpc.NewServer(grpc.Creds(credentials))
	runner := &observedRunner{bootID: "proxy-test-boot"}
	runnerservicepb.RegisterRunnerServer(runnerServer, runner)
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
	client := runnerservicepb.NewRunnerClient(proxy)
	if _, err := client.GetStatus(f.callerContext(f.appToken), &runnerpb.GetStatusRequest{}); err != nil {
		t.Fatalf("authorized app call did not reach Runner: %v", err)
	}
	if runner.calls.Load() != 1 {
		t.Fatal("authorized app call did not reach the direct Runner server")
	}
	if _, err := client.GetStatus(f.callerContext(f.executorToken), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("caller outside the Runner proxy allowlist was accepted: %v", err)
	}

	staleContext := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Bearer "+f.appToken,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, "replaced-sandbox-uid",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	if _, err := client.GetStatus(staleContext, &runnerpb.GetStatusRequest{}); status.Code(err) != codes.NotFound {
		t.Fatalf("proxy accepted a stale Sandbox UID: %v", err)
	}

	// The controller can still seal a live Runner while a held suspend is visible in the
	// Gateway's cache. Other proxy methods remain unavailable in that phase.
	f.sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	f.sandbox.Status.StopStage = ""
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetStatus(f.callerContext(f.appToken), &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus in held suspend = %v, want Unavailable", err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("controller Quiesce in held suspend did not reach Runner: %v", err)
	}
	if got := runner.quiesceCalls.Load(); got != 1 {
		t.Fatalf("Runner received %d Quiesce calls, want 1", got)
	}
	if _, err := client.Quiesce(f.callerContext(f.appToken), &runnerpb.QuiesceRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("app Quiesce in held suspend = %v, want PermissionDenied", err)
	}
	f.sandbox.Status.Incarnation.UID = "replaced-runner-uid"
	if err := f.objects.Status().Update(t.Context(), f.sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &runnerpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
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
	if _, err := client.Quiesce(f.callerContext(f.controllerToken), &runnerpb.QuiesceRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Quiesce with an unready Pod = %v, want Unavailable", err)
	}
	if got := runner.quiesceCalls.Load(); got != 1 {
		t.Fatalf("unready Pod sent a second Quiesce to Runner: %d", got)
	}

	stopObserver()
	select {
	case <-observerDone:
	case <-time.After(time.Second):
		t.Fatal("Runner observer did not stop")
	}
}
