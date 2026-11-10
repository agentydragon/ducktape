package gateway

import (
	"context"
	"fmt"
	"maps"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
	ctrlcache "sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type observedRunner struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	bootID       string
	calls        atomic.Int32
	quiesceCalls atomic.Int32
}

func (r *observedRunner) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	if err := stream.Send(&environmentruntimepb.WatchStateResponse{
		SandboxUid:               request.SandboxUid,
		EnvironmentRuntimeBootId: r.bootID, IncarnationUid: request.IncarnationUid, ConnectionEpoch: request.ConnectionEpoch,
		Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{Bootstrap: environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED},
	}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (r *observedRunner) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("agentplane-environment-runtime-boot-id")) != 1 || md.Get("agentplane-environment-runtime-boot-id")[0] != r.bootID ||
		len(md.Get("agentplane-sandbox-uid")) != 1 || len(md.Get("agentplane-incarnation-uid")) != 1 ||
		len(md.Get("agentplane-connection-epoch")) != 1 {
		return nil, status.Error(codes.PermissionDenied, "missing Runner request fence")
	}
	r.calls.Add(1)
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

func (r *observedRunner) Quiesce(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	r.quiesceCalls.Add(1)
	return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}, nil
}

func TestRunnerObserverPublishesFactsAndFencesRPCs(t *testing.T) {
	for _, mode := range []environment_runtime_transport.SecurityMode{environment_runtime_transport.MutualTLS, environment_runtime_transport.Insecure} {
		t.Run(string(mode), func(t *testing.T) {
			testRunnerObserver(t, mode)
		})
	}
}

// pauseAfterBootIDUpdate holds the first externally ready Lease update's response
// after the API server has made it visible to other clients.
type pauseAfterBootIDUpdate struct {
	coordinationclient.CoordinationV1Interface
	updated chan struct{}
	release <-chan struct{}
	paused  atomic.Bool
}

func (p *pauseAfterBootIDUpdate) Leases(namespace string) coordinationclient.LeaseInterface {
	return &pausedBootIDLeases{LeaseInterface: p.CoordinationV1Interface.Leases(namespace), parent: p}
}

type pausedBootIDLeases struct {
	coordinationclient.LeaseInterface
	parent *pauseAfterBootIDUpdate
}

type inspectReleaseCoordination struct {
	coordinationclient.CoordinationV1Interface
	inspect func()
}

func (c *inspectReleaseCoordination) Leases(namespace string) coordinationclient.LeaseInterface {
	return &inspectReleaseLeases{LeaseInterface: c.CoordinationV1Interface.Leases(namespace), inspect: c.inspect}
}

type inspectReleaseLeases struct {
	coordinationclient.LeaseInterface
	inspect func()
}

func (l *inspectReleaseLeases) Update(ctx context.Context, lease *coordinationv1.Lease, options metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	if lease.Spec.HolderIdentity == nil {
		l.inspect()
	}
	return l.LeaseInterface.Update(ctx, lease, options)
}

func (p *pausedBootIDLeases) Update(ctx context.Context, lease *coordinationv1.Lease, options metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	updated, err := p.LeaseInterface.Update(ctx, lease, options)
	if err == nil && updated.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" && p.parent.paused.CompareAndSwap(false, true) {
		close(p.parent.updated)
		select {
		case <-p.parent.release:
		case <-ctx.Done():
		}
	}
	return updated, err
}

func startObservedRunner(t *testing.T, f *sandboxFixture, g *Gateway, runner environmentruntimepb.EnvironmentRuntimeServer) runnerDestination {
	t.Helper()
	destination, err := g.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	creds, err := (environment_runtime_transport.Security{Mode: environment_runtime_transport.MutualTLS, TLS: &f.runnerTLS}).ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	destination.target = listener.Addr().String()
	return destination
}

type delayedFirstStateRunner struct {
	*observedRunner
	watchStarted chan struct{}
	release      chan struct{}
}

func (r *delayedFirstStateRunner) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	close(r.watchStarted)
	select {
	case <-r.release:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	return r.observedRunner.WatchState(request, stream)
}

func TestFirstRunnerBootIDIsPublishedAfterLocalConnection(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30")
	runner := &observedRunner{bootID: "first-boot"}
	destination := startObservedRunner(t, f, g, runner)

	updated := make(chan struct{})
	release := make(chan struct{})
	g.records.coordination = &pauseAfterBootIDUpdate{CoordinationV1Interface: g.records.coordination, updated: updated, release: release}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- g.observeRunner(ctx, destination) }()
	select {
	case <-updated:
	case err := <-finished:
		t.Fatalf("Runner observer stopped before publishing boot ID: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("Runner boot ID was not published")
	}

	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.EnvironmentRuntimeLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.PodUIDAnnotation] != string(f.pod.UID) ||
		lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != runner.bootID ||
		lease.Annotations[sandboxlease.BootstrapAnnotation] != "" ||
		lease.Spec.RenewTime == nil || lease.Spec.LeaseTransitions == nil {
		t.Fatalf("first ready Lease update has unexpected facts: %#v", lease)
	}
	route, err := api.Clientset.DiscoveryV1().EndpointSlices(f.namespace).Get(t.Context(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if route.Annotations[sandboxlease.ConnectionEpochAnnotation] != strconv.Itoa(int(*lease.Spec.LeaseTransitions)) ||
		route.Annotations[sandboxlease.ConnectionLeaseUIDAnnotation] != string(lease.UID) ||
		len(route.Endpoints) != 1 || len(route.Endpoints[0].Addresses) != 1 || route.Endpoints[0].Addresses[0] != g.config.HolderIP ||
		route.Endpoints[0].Conditions.Ready == nil || !*route.Endpoints[0].Conditions.Ready {
		t.Fatalf("control route is not ready at boot-ID publication: %#v", route)
	}
	connection, err := g.connections.holder(string(f.sandbox.UID))
	if err != nil || connection.epoch != uint64(*lease.Spec.LeaseTransitions) || connection.podUID != string(f.pod.UID) || connection.bootID != runner.bootID {
		t.Fatalf("first ready Lease update preceded a correctly fenced local connection: %v, %+v", err, connection)
	}
	requestMetadata, _ := metadata.FromOutgoingContext(f.callerContext(f.appToken))
	_, err = (&environmentRuntimeProxy{gateway: g}).GetStatus(metadata.NewIncomingContext(t.Context(), requestMetadata), &sessionpb.GetStatusRequest{})
	if err != nil || runner.calls.Load() != 1 {
		t.Fatalf("GetStatus at first ready Lease update = %v, runner calls = %d", err, runner.calls.Load())
	}
	close(release)
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	for {
		select {
		case err := <-finished:
			t.Fatalf("Runner observer stopped before publishing facts: %v", err)
		case <-deadline.C:
			t.Fatal("Runner facts were not published after releasing boot-ID update")
		case <-poll.C:
			lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), lease.Name, metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if lease.Annotations[sandboxlease.BootstrapAnnotation] == "succeeded" {
				cancel()
				return
			}
		}
	}
}

type failBootIDUpdate struct {
	coordinationclient.CoordinationV1Interface
	local      *connections
	sandboxUID string
	checked    chan error
}

func (f *failBootIDUpdate) Leases(namespace string) coordinationclient.LeaseInterface {
	return &failingBootIDLeases{LeaseInterface: f.CoordinationV1Interface.Leases(namespace), parent: f}
}

type failingBootIDLeases struct {
	coordinationclient.LeaseInterface
	parent *failBootIDUpdate
}

func (f *failingBootIDLeases) Update(ctx context.Context, lease *coordinationv1.Lease, options metav1.UpdateOptions) (*coordinationv1.Lease, error) {
	if lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" {
		_, err := f.parent.local.holder(f.parent.sandboxUID)
		f.parent.checked <- err
		return nil, status.Error(codes.Internal, "injected boot-ID update rejection")
	}
	return f.LeaseInterface.Update(ctx, lease, options)
}

func TestRejectedBootIDPublicationRemovesLocalConnection(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30")
	destination := startObservedRunner(t, f, g, &observedRunner{bootID: "first-boot"})
	checked := make(chan error, 1)
	g.records.coordination = &failBootIDUpdate{
		CoordinationV1Interface: g.records.coordination, local: g.connections,
		sandboxUID: string(f.sandbox.UID), checked: checked,
	}
	finished := make(chan error, 1)
	go func() { finished <- g.observeRunner(t.Context(), destination) }()
	select {
	case err := <-checked:
		if err != nil {
			t.Fatalf("local connection was unavailable before boot-ID publication: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("boot-ID update was never attempted")
	}
	select {
	case err := <-finished:
		if status.Code(err) != codes.Internal {
			t.Fatalf("boot-ID update rejection = %v, want Internal", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("observer did not stop after boot-ID update rejection")
	}
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("rejected observation left a local connection callable: %v", err)
	}
	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.EnvironmentRuntimeLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" {
		t.Fatalf("rejected observation published readiness: %#v", lease.Annotations)
	}
}

func TestSupersededClaimCannotInstallConnectionBeforeBootID(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30")
	runner := &delayedFirstStateRunner{
		observedRunner: &observedRunner{bootID: "first-boot"},
		watchStarted:   make(chan struct{}), release: make(chan struct{}),
	}
	destination := startObservedRunner(t, f, g, runner)
	finished := make(chan error, 1)
	go func() { finished <- g.observeRunner(t.Context(), destination) }()
	select {
	case <-runner.watchStarted:
	case err := <-finished:
		t.Fatalf("observer stopped before first watch: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("observer did not start its first watch")
	}
	newEpoch, _, err := g.records.claim(t.Context(), destination.identity, destination.podUID)
	if err != nil || newEpoch < 2 {
		t.Fatalf("failed to supersede the original claim: epoch %d, %v", newEpoch, err)
	}
	close(runner.release)
	select {
	case err := <-finished:
		if status.Code(err) != codes.Aborted {
			t.Fatalf("superseded observer = %v, want Aborted", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("superseded observer did not stop")
	}
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("superseded observer installed a local connection: %v", err)
	}
	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.EnvironmentRuntimeLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != "" || lease.Spec.LeaseTransitions == nil || uint64(*lease.Spec.LeaseTransitions) != newEpoch {
		t.Fatalf("superseded observer published readiness: %#v", lease)
	}
}

func TestLocalConnectionsRejectStaleObserverEpochs(t *testing.T) {
	uid := "sandbox-uid"
	identity := &sandboxreader.Identity{Uid: uid}
	local := newConnections()
	first := &runnerConnection{identity: identity, epoch: 1, ended: make(chan struct{})}
	newer := &runnerConnection{identity: identity, epoch: 2, ended: make(chan struct{})}
	if err := local.accept(first); err != nil {
		t.Fatal(err)
	}
	if err := local.accept(newer); err != nil {
		t.Fatal(err)
	}
	for _, candidate := range []*runnerConnection{
		{identity: identity, epoch: 1, ended: make(chan struct{})},
		{identity: identity, epoch: 2, ended: make(chan struct{})},
	} {
		if err := local.accept(candidate); status.Code(err) != codes.Aborted {
			t.Fatalf("accepted an observer at stale or duplicate epoch %d: %v", candidate.epoch, err)
		}
	}
	if current, err := local.holder(uid); err != nil || current != newer {
		t.Fatalf("stale observer displaced the newer connection: %v, %+v", err, current)
	}
	local.replaceLease("", uid, 4)
	if err := local.accept(&runnerConnection{identity: identity, epoch: 3, ended: make(chan struct{})}); status.Code(err) != codes.Aborted {
		t.Fatalf("accepted an observer superseded by watched Lease epoch: %v", err)
	}
}

func TestUnavailableLocalConnectionDoesNotClaimAnotherReplicaOwnsIt(t *testing.T) {
	uid := "sandbox-uid"
	identity := &sandboxreader.Identity{Uid: uid}
	local := newConnections()

	if _, err := local.holder(uid); status.Code(err) != codes.Unavailable || status.Convert(err).Message() != runnerConnectionUnavailable {
		t.Fatalf("missing local connection error = %v, want Unavailable %q", err, runnerConnectionUnavailable)
	}

	connection := &runnerConnection{identity: identity, epoch: 1, ended: make(chan struct{})}
	if err := local.accept(connection); err != nil {
		t.Fatal(err)
	}
	local.replaceLease("", uid, 2)
	if _, err := local.holder(uid); status.Code(err) != codes.Unavailable || status.Convert(err).Message() != runnerConnectionUnavailable {
		t.Fatalf("connection behind watched Lease error = %v, want Unavailable %q", err, runnerConnectionUnavailable)
	}
}

func testRunnerObserver(t *testing.T, mode environment_runtime_transport.SecurityMode) {
	t.Helper()
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	g.config.RunnerSecurity = environment_runtime_transport.Security{Mode: mode}
	runnerSecurity := environment_runtime_transport.Security{Mode: mode}
	if mode == environment_runtime_transport.MutualTLS {
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
	creds, err := runnerSecurity.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	runner := &observedRunner{bootID: "test-runner-boot"}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
	executorpb.RegisterExecutorServer(server, &echoForwardExecutor{})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	destination.target = listener.Addr().String()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	var connection *runnerConnection
	var observedConnection atomic.Pointer[runnerConnection]
	releaseChecked := atomic.Bool{}
	g.records.coordination = &inspectReleaseCoordination{CoordinationV1Interface: g.records.coordination, inspect: func() {
		if ctx.Err() == nil {
			t.Error("Lease released before old observer watch context was canceled")
		}
		if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
			t.Errorf("Lease released while old connection was locally callable: %v", err)
		}
		old := observedConnection.Load()
		if old == nil || old.direct.conn.GetState() != connectivity.Shutdown {
			t.Error("Lease released before direct Runner connection closed")
		}
		releaseChecked.Store(true)
	}}
	finished := make(chan error, 1)
	go func() { finished <- g.observeRunner(ctx, destination) }()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
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
	observedConnection.Store(connection)
	if _, err := connection.runner.GetStatus(t.Context(), &sessionpb.GetStatusRequest{}); err != nil {
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
	exec, err := executorpb.NewExecutorClient(publicConn).OpenSession(execCtx, &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
		},
		RequestId: "open-session-request",
	})
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
	quiescing := f.sandbox.DeepCopy()
	quiescing.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	quiescing.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := f.objects.Status().Update(t.Context(), quiescing); err != nil {
		t.Fatal(err)
	}
	started, err := executorpb.NewExecutorClient(publicConn).StartProcess(execCtx, &executorpb.StartProcessRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "test-runner-exec-session",
		},
		ExecutionSessionId: opened.GetExecutionSessionId(), RequestId: "start-after-quiesce", ProcessId: "proc-1",
	})
	if err != nil || started.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("existing-session unary call during quiescing = %v, %v", started, err)
	}
	stopExec()
	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(t.Context(), testSandbox+sandboxlease.EnvironmentRuntimeLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] != runner.bootID || lease.Annotations[sandboxlease.PodUIDAnnotation] != string(f.pod.UID) {
		t.Fatalf("Runner connection did not publish its identities: %v", lease.Annotations)
	}
	other := f.newGateway(t, "test-other-gateway", "192.0.2.31")
	if _, _, err := other.records.claim(t.Context(), destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("another live Gateway stole Runner ownership: %v", err)
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("Runner observation did not stop")
	}
	if !releaseChecked.Load() {
		t.Fatal("Runner observer stopped without releasing its Lease")
	}
	if _, err := g.connections.holder(string(f.sandbox.UID)); status.Code(err) != codes.Unavailable {
		t.Fatalf("closed Runner connection remained callable: %v", err)
	}
	newEpoch, _, err := other.records.claim(t.Context(), destination.identity, destination.podUID)
	if err != nil || newEpoch <= connection.epoch {
		t.Fatalf("stopped Gateway did not hand off the runner Lease immediately: epoch=%d, err=%v", newEpoch, err)
	}
	route, err := api.Clientset.DiscoveryV1().EndpointSlices(f.namespace).Get(t.Context(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(route.Endpoints) != 1 || len(route.Endpoints[0].Addresses) != 1 || route.Endpoints[0].Addresses[0] != "192.0.2.31" {
		t.Fatalf("control route did not follow the new holder: %+v", route.Endpoints)
	}
}

func TestExecutionConnectionOnlySignalsVerifiedIncarnationChanges(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	server := &executorServer{gateway: g}
	scope := &executorpb.ExecutionScope{
		Destination: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
		},
		ExpectedIncarnationUid: "previous-incarnation", SessionId: "sampler-session",
	}
	var epoch uint64
	setConnection := func(podUID string) {
		epoch++
		if err := g.connections.accept(&runnerConnection{
			identity: &sandboxreader.Identity{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)},
			podUID:   podUID, epoch: epoch, ended: make(chan struct{}),
		}); err != nil {
			t.Fatal(err)
		}
	}
	setConnection("previous-incarnation")

	_, _, _, err := server.executionConnection(t.Context(), scope, false)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale execution scope = %v, want FailedPrecondition", err)
	}
	for _, detail := range status.Convert(err).Details() {
		if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
			t.Fatalf("stale Gateway holder carried a rebind detail: %v", detail)
		}
	}
	setConnection(string(f.pod.UID))
	_, _, _, err = server.executionConnection(t.Context(), scope, false)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale execution scope with current holder = %v, want FailedPrecondition", err)
	}
	var changed *executorpb.SandboxIncarnationChanged
	for _, detail := range status.Convert(err).Details() {
		if candidate, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
			changed = candidate
		}
	}
	if changed == nil || changed.GetSandboxUid() != string(f.sandbox.UID) || changed.GetIncarnationUid() != string(f.pod.UID) {
		t.Fatalf("stale execution details = %v, want verified Sandbox %q and incarnation %q", status.Convert(err).Details(), f.sandbox.UID, f.pod.UID)
	}

	for _, test := range []struct {
		name   string
		scope  *executorpb.ExecutionScope
		mutate func(*sandboxapi.Sandbox)
		code   codes.Code
	}{
		{name: "invalid scope", scope: &executorpb.ExecutionScope{Destination: scope.Destination, SessionId: scope.SessionId}, code: codes.InvalidArgument},
		{name: "foreign Sandbox", scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: "another-sandbox"}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: scope.ExpectedIncarnationUid, SessionId: scope.SessionId,
		}, code: codes.NotFound},
		{name: "unavailable incarnation", scope: scope, mutate: func(sandbox *sandboxapi.Sandbox) {
			sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
			sandbox.Status.Incarnation = nil
		}, code: codes.Unavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.mutate != nil {
				sandbox := f.sandbox.DeepCopy()
				test.mutate(sandbox)
				if err := f.objects.Status().Update(t.Context(), sandbox); err != nil {
					t.Fatal(err)
				}
			}
			_, _, _, err := server.executionConnection(t.Context(), test.scope, false)
			if status.Code(err) != test.code {
				t.Fatalf("execution scope = %v, want %s", err, test.code)
			}
			for _, detail := range status.Convert(err).Details() {
				if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
					t.Fatalf("rejected request carried a rebind detail: %v", detail)
				}
				if test.name == "invalid scope" || test.name == "foreign Sandbox" {
					if _, ok := detail.(*executorpb.ExecutionEnvironmentFailure); ok {
						t.Fatalf("unverified destination carried environment state: %v", detail)
					}
				}
			}
		})
	}
}

func TestExecutionConnectionReportsVerifiedEnvironmentFailures(t *testing.T) {
	for _, test := range []struct {
		name       string
		state      executorpb.ExecutionEnvironmentFailure_State
		code       codes.Code
		newSession bool
		mutate     func(*sandboxFixture) error
	}{
		{
			name: "suspended", state: executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
				sandbox.Status.Incarnation = nil
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "starting", state: executorpb.ExecutionEnvironmentFailure_STATE_STARTING, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.Incarnation = nil
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "not ready", state: executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY, code: codes.Unavailable,
			mutate: func(f *sandboxFixture) error {
				pod := f.pod.DeepCopy()
				pod.Status.Conditions[0].Status = corev1.ConditionFalse
				_, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{})
				return err
			},
		},
		{
			name: "quiescing admission", state: executorpb.ExecutionEnvironmentFailure_STATE_QUIESCING, code: codes.FailedPrecondition,
			newSession: true,
			mutate: func(f *sandboxFixture) error {
				sandbox := f.sandbox.DeepCopy()
				sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
				sandbox.Status.StopStage = sandboxapi.StopStageQuiescing
				return f.objects.Status().Update(t.Context(), sandbox)
			},
		},
		{
			name: "control route unavailable", state: executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE, code: codes.Unavailable,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newSandboxFixture(t)
			if test.mutate != nil {
				if err := test.mutate(f); err != nil {
					t.Fatal(err)
				}
			}
			g := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
			server := &executorServer{gateway: g}
			scope := &executorpb.ExecutionScope{
				Destination: &identitypb.SandboxDestination{
					Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
				},
				ExpectedIncarnationUid: string(f.pod.UID), SessionId: "sampler-session",
			}
			_, _, _, err := server.executionConnection(t.Context(), scope, test.newSession)
			if status.Code(err) != test.code {
				t.Fatalf("execution connection error=%v, want %s", err, test.code)
			}
			var failure *executorpb.ExecutionEnvironmentFailure
			for _, detail := range status.Convert(err).Details() {
				if candidate, ok := detail.(*executorpb.ExecutionEnvironmentFailure); ok {
					failure = candidate
				}
				if _, ok := detail.(*executorpb.SandboxIncarnationChanged); ok {
					t.Fatalf("environment failure disclosed a replacement incarnation: %v", detail)
				}
			}
			if failure == nil || failure.GetState() != test.state || failure.GetOutcome() != executorpb.ExecutionEnvironmentFailure_OUTCOME_NOT_DISPATCHED {
				t.Fatalf("execution environment details=%v, want state %s and not-dispatched", status.Convert(err).Details(), test.state)
			}
		})
	}
}

type echoForwardExecutor struct {
	executorpb.UnimplementedExecutorServer
}

func (*echoForwardExecutor) OpenSession(_ *executorpb.OpenSessionRequest, stream grpc.ServerStreamingServer[executorpb.OpenSessionResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	if len(md.Get("agentplane-environment-runtime-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
		len(md.Get("agentplane-incarnation-uid")) != 1 || len(md.Get("agentplane-connection-epoch")) != 1 {
		return status.Error(codes.PermissionDenied, "missing executor fence")
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
	if len(md.Get("agentplane-environment-runtime-boot-id")) != 1 || len(md.Get("agentplane-sandbox-uid")) != 1 ||
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
	pod.Labels[sandboxapi.EnvironmentRuntimeCredentialLabel] = "test-replacement-credential"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).Update(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := g.validateRunnerDestination(t.Context(), destination); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old certificate followed replacement: %v", err)
	}
	newURI, err := environment_runtime_transport.WorkloadID{Namespace: f.namespace, Name: f.sandbox.Name, CredentialID: "test-replacement-credential"}.URI()
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
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	observations atomic.Int32
	disconnect   chan struct{}
}

func (r *reconnectingRunner) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	observation := r.observations.Add(1)
	if err := stream.Send(&environmentruntimepb.WatchStateResponse{
		SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid,
		ConnectionEpoch: request.ConnectionEpoch, EnvironmentRuntimeBootId: fmt.Sprintf("boot-%d", observation),
		Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
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

func (r *reconnectingRunner) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if got := md.Get("agentplane-environment-runtime-boot-id"); len(got) != 1 || got[0] != fmt.Sprintf("boot-%d", r.observations.Load()) {
		return nil, status.Error(codes.Unavailable, "stale runner boot")
	}
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

func TestRunnerObserverReconnectsBeforeNextDiscoveryAndFencesOldBoot(t *testing.T) {
	f := newSandboxFixture(t)
	pod := f.pod.DeepCopy()
	pod.Status.PodIP = "127.0.0.1"
	if _, err := api.Clientset.CoreV1().Pods(f.namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	g := f.newGateway(t, "test-reconnecting-gateway", "192.0.2.30")
	g.config.RunnerSecurity = environment_runtime_transport.Security{Mode: environment_runtime_transport.Insecure}
	// The Sandbox's only event: transport loss must trigger its own retry.
	g.RunnerDiscoveryHandler().OnAdd(f.sandbox, true)
	listener, err := net.Listen("tcp", "127.0.0.1:9090")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runner := &reconnectingRunner{disconnect: make(chan struct{})}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
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
	if _, err := second.runner.GetStatus(ctx, &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("replacement Runner did not serve calls: %v", err)
	}
	stale := &fencedRunnerConn{conn: second.direct.conn, identity: first.direct.identity, connection: first}
	if _, err := environmentruntimepb.NewEnvironmentRuntimeClient(stale).GetStatus(ctx, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old boot remained usable after reconnect: %v", err)
	}
}

func TestRunnerObserverKeepsUnchangedDestinationOnSandboxUpdate(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-stable-gateway", "192.0.2.30")
	destination, err := g.runnerDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	// A status update causes another read of the same identity into a new protobuf value.
	updated := f.sandbox.DeepCopy()
	updated.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	updated.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := f.objects.Status().Update(t.Context(), updated); err != nil {
		t.Fatal(err)
	}
	oldDone := make(chan struct{})
	cancelled := false
	active := map[string]runnerObserver{
		f.sandbox.Name: {destination: destination, cancel: func() { cancelled = true }, done: oldDone},
	}
	ctx, cancel := context.WithCancel(t.Context())
	var workers sync.WaitGroup
	defer workers.Wait()
	defer cancel()
	if err := g.reconcileObserver(ctx, f.sandbox.Name, active, &workers); err != nil {
		t.Fatal(err)
	}
	if cancelled || active[f.sandbox.Name].done != oldDone {
		t.Fatal("an unchanged destination replaced the live runner observer")
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
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	heartbeats atomic.Int32
}

func (r *heartbeatingRunner) WatchState(request *environmentruntimepb.WatchStateRequest, stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse]) error {
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := stream.Send(&environmentruntimepb.WatchStateResponse{
			SandboxUid: request.SandboxUid, IncarnationUid: request.IncarnationUid, ConnectionEpoch: request.ConnectionEpoch,
			EnvironmentRuntimeBootId: "test-heartbeating-boot", Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
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
	g.config.RunnerSecurity = environment_runtime_transport.Security{Mode: environment_runtime_transport.Insecure}
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
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(sandboxapi.EnvironmentRuntimeServicePort)))
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runner := &heartbeatingRunner{}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
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
	connection.runner = environmentruntimepb.NewEnvironmentRuntimeClient(connection.direct)
	connection.end(errSupersededConnection)
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.runner.GetStatus(t.Context(), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Aborted {
		t.Fatalf("call on an ended connection = %v, want the connection's end (Aborted)", err)
	}
}
