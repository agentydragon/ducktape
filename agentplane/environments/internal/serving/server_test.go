package serving

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

const (
	servingTestNamespace = "environment-serving-test"
	testBearerAudience   = "environment-serving-test"
	sandboxName          = "replica-race"
	initialGeneration    = int64(8)
)

type leaderProbe struct {
	mu      sync.Mutex
	active  int
	started chan struct{}
}

func newLeaderProbe() *leaderProbe { return &leaderProbe{started: make(chan struct{}, 1)} }

func (*leaderProbe) NeedLeaderElection() bool { return true }

func (p *leaderProbe) Start(ctx context.Context) error {
	p.mu.Lock()
	p.active++
	p.mu.Unlock()
	select {
	case p.started <- struct{}{}:
	default:
	}
	<-ctx.Done()
	p.mu.Lock()
	p.active--
	p.mu.Unlock()
	return nil
}

func (p *leaderProbe) isActive() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.active != 0
}

type replica struct {
	server   *Server
	probe    *leaderProbe
	cancel   context.CancelFunc
	done     chan error
	stopOnce sync.Once
	stopped  chan struct{}
	stopErr  error
}

func TestEveryReplicaServesAndFencesConcurrentResume(t *testing.T) {
	kube := kubeapiserver.StartForTest(t)
	if _, err := kube.Clientset.CoreV1().Namespaces().Create(t.Context(), &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: servingTestNamespace},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}

	scheme := testscheme.New(t)
	objects, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	identityName := "delegated-owner"
	policyIdentity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: servingTestNamespace, Name: identityName},
		Spec:       policyapi.IdentitySpec{Description: "Environment serving test Identity"},
	}
	if err := objects.Create(t.Context(), policyIdentity); err != nil {
		t.Fatal(err)
	}
	policySet := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: servingTestNamespace, Name: "delegated-resume"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: "environment", Operations: []string{"resume"},
			Environments: []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Self: true}}},
		}}},
	}
	if err := objects.Create(t.Context(), policySet); err != nil {
		t.Fatal(err)
	}
	policyBinding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: servingTestNamespace, Name: "delegated-resume"},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identityName}},
			PolicySets: []string{policySet.Name},
		},
	}
	if err := objects.Create(t.Context(), policyBinding); err != nil {
		t.Fatal(err)
	}
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: servingTestNamespace, Name: sandboxName},
		Spec: sandboxapi.SandboxSpec{
			Template: "test-template", Running: true, WakeGeneration: initialGeneration,
			Identity: &sandboxapi.IdentityRef{Name: identityName},
		},
	}
	if err := objects.Create(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	idle := sandboxapi.SuspendedByIdle
	suspendedAt := initialGeneration
	sandbox.Status = sandboxapi.SandboxStatus{
		OperatingMode:             sandboxapi.OperatingModeSuspended,
		StopStage:                 sandboxapi.StopStageStopped,
		SuspendedBy:               &idle,
		SuspendedAtWakeGeneration: &suspendedAt,
	}
	if err := objects.Status().Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}

	ca := peertlstesting.NewCA(t)
	serverCertificate := ca.Issue(t, peertls.SandboxControllerURI)
	runtimeProxyCertificate := ca.Issue(t, peertls.RuntimeProxyURI)
	c := Config{
		RPC: listener.Config{
			ListenAddress: "127.0.0.1:0",
			TLS:           &listener.TLS{CertFile: serverCertificate.CertFile, KeyFile: serverCertificate.KeyFile},
			Auth: []*listener.SPIFFEAuth{
				{CAFile: ca.RootFile},
			},
		},
		PolicyNamespace:  servingTestNamespace,
		ServiceReaders:   []string{peertls.CentralSamplerURI},
		ServiceOperators: []string{peertls.ActionsURI},
		ServiceDelegation: &auth.ServiceDelegationConfig{
			Actors:  []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}},
			Methods: []string{environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName},
		},
		RuntimeProxy: &RuntimeProxyConfig{
			RunnerSecurity: peertls.Security{
				Mode: peertls.MutualTLS, TLS: &runtimeProxyCertificate,
			},
			Readers:           []string{peertls.AppURI},
			SessionCallers:    []string{peertls.SessionsURI},
			ControllerCallers: []string{peertls.SandboxControllerURI},
			ExecutorCallers:   []string{peertls.CentralSamplerURI},
		},
	}
	leaseDuration, renewDeadline, retryPeriod := 6*time.Second, 4*time.Second, time.Second
	startReplica := func() *replica {
		t.Helper()
		server, err := New(c, servingTestNamespace, kube.Config, scheme, reader.KubernetesIdentities{})
		if err != nil {
			t.Fatal(err)
		}
		manager, err := ctrl.NewManager(kube.Config, ctrl.Options{
			Scheme:  scheme,
			Metrics: metricsserver.Options{BindAddress: "0"}, HealthProbeBindAddress: "0",
			LeaderElection: true, LeaderElectionID: "environment-serving-integration",
			LeaderElectionNamespace: servingTestNamespace, LeaderElectionReleaseOnCancel: true,
			LeaseDuration: &leaseDuration, RenewDeadline: &renewDeadline, RetryPeriod: &retryPeriod,
		})
		if err != nil {
			t.Fatal(err)
		}
		probe := newLeaderProbe()
		if err := manager.Add(server); err != nil {
			t.Fatal(err)
		}
		if err := manager.Add(probe); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithCancel(t.Context())
		r := &replica{server: server, probe: probe, cancel: cancel, done: make(chan error, 1), stopped: make(chan struct{})}
		go func() { r.done <- manager.Start(ctx) }()
		return r
	}
	first, second := startReplica(), startReplica()
	stopReplica := func(r *replica) {
		t.Helper()
		r.stopOnce.Do(func() {
			r.cancel()
			select {
			case r.stopErr = <-r.done:
			case <-time.After(10 * time.Second):
				r.stopErr = errors.New("serving manager did not stop within 10 seconds")
			}
			close(r.stopped)
		})
		<-r.stopped
		if r.stopErr != nil {
			t.Errorf("stop serving manager: %v", r.stopErr)
		}
	}
	t.Cleanup(func() { stopReplica(first) })
	t.Cleanup(func() { stopReplica(second) })
	eventually.Eventually(t, "the first replica to be ready", func() bool { return first.server.Check(nil) == nil })
	eventually.Eventually(t, "the second replica to be ready", func() bool { return second.server.Check(nil) == nil })

	// A valid Kubernetes service-account bearer still gets no EnvironmentService admission.
	bearerToken, err := kube.WorkloadToken(t.Context(), auth.Account{Namespace: servingTestNamespace, Name: "bearer-only"}, testBearerAudience)
	if err != nil {
		t.Fatal(err)
	}
	security, err := (&peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.SandboxControllerURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	bearerConnection, err := grpc.NewClient(first.server.Address(), grpc.WithNoProxy(), grpc.WithTransportCredentials(security))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bearerConnection.Close() })
	bearerClient := environmentpb.NewEnvironmentServiceClient(bearerConnection)
	bearerCtx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+bearerToken)
	if _, err := bearerClient.ListEnvironments(bearerCtx, &environmentpb.ListEnvironmentsRequest{Limit: 10}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer admitted: %v", err)
	}

	firstClient, firstConnection := environmentClient(t, ca, first.server)
	secondClient, secondConnection := environmentClient(t, ca, second.server)
	t.Cleanup(func() { _ = firstConnection.Close() })
	t.Cleanup(func() { _ = secondConnection.Close() })
	assertRuntimeProxyAuthorization(t, ca, first.server)
	assertRuntimeProxyAuthorization(t, ca, second.server)
	target := &identitypb.EnvironmentTarget{Namespace: servingTestNamespace, Name: sandboxName, Uid: string(sandbox.UID)}
	request := resumeRequest(target, initialGeneration)

	// The same guarded intent through both replicas may advance only one generation.
	start := make(chan struct{})
	type result struct {
		response *environmentpb.ResumeEnvironmentResponse
		err      error
	}
	results := make(chan result, 2)
	for _, rpc := range []environmentpb.EnvironmentServiceClient{firstClient, secondClient} {
		go func(rpc environmentpb.EnvironmentServiceClient) {
			<-start
			ctx, cancel := callContext(t)
			defer cancel()
			response, err := rpc.ResumeEnvironment(ctx, request)
			results <- result{response: response, err: err}
		}(rpc)
	}
	close(start)
	requested, changed := 0, 0
	for range 2 {
		got := <-results
		if got.err != nil {
			t.Fatalf("concurrent resume failed: %v", got.err)
		}
		switch got.response.GetOutcome() {
		case environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED:
			requested++
		case environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED:
			changed++
		default:
			t.Fatalf("concurrent resume outcome = %v", got.response.GetOutcome())
		}
	}
	if requested != 1 || changed != 1 {
		t.Fatalf("concurrent resume outcomes requested=%d state_changed=%d", requested, changed)
	}
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	if !sandbox.Spec.Running || sandbox.Spec.WakeGeneration != initialGeneration+1 {
		t.Fatalf("guarded resume state running=%t generation=%d", sandbox.Spec.Running, sandbox.Spec.WakeGeneration)
	}

	staleTarget := &identitypb.EnvironmentTarget{Namespace: target.Namespace, Name: target.Name, Uid: "stale-sandbox-uid"}
	callCtx, cancel := callContext(t)
	_, err = firstClient.ResumeEnvironment(callCtx, resumeRequest(staleTarget, initialGeneration))
	cancel()
	if status.Code(err) != codes.NotFound {
		t.Fatalf("stale Sandbox UID returned %v, want NotFound", err)
	}
	callCtx, cancel = callContext(t)
	stale, err := secondClient.ResumeEnvironment(callCtx, request)
	cancel()
	if err != nil || stale.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED || stale.GetWakeGeneration() != initialGeneration+1 {
		t.Fatalf("stale resume token = %v, %v", stale, err)
	}

	wrongServiceClient := clientFor(t, ca, first.server, peertls.SessionsURI)
	callCtx, cancel = callContext(t)
	wrongService, err := wrongServiceClient.ResumeEnvironment(callCtx, request)
	cancel()
	if status.Code(err) != codes.PermissionDenied || wrongService != nil {
		t.Fatalf("unconfigured service resume = %v, %v; want PermissionDenied", wrongService, err)
	}
	callCtx, cancel = callContext(t)
	wrongMethod, err := firstClient.GetEnvironment(callCtx, &environmentpb.GetEnvironmentRequest{Target: target})
	cancel()
	if status.Code(err) != codes.PermissionDenied || wrongMethod != nil {
		t.Fatalf("operator read method = %v, %v; want PermissionDenied", wrongMethod, err)
	}
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	if sandbox.Spec.WakeGeneration != initialGeneration+1 {
		t.Fatalf("denied requests changed wake generation to %d", sandbox.Spec.WakeGeneration)
	}

	delegatedClient, delegatedConnection := delegatedEnvironmentClient(t, ca, second.server, policyIdentity)
	t.Cleanup(func() { _ = delegatedConnection.Close() })
	delegatedRequest := resumeRequest(target, initialGeneration+1)
	eventuallyResumeOutcome(t, delegatedClient, delegatedRequest, environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING)

	// The readiness check and delegated authorization both follow the replica-local policy watch health.
	second.server.policyHealth.RecordFailure("policysets", errors.New("injected policy watch failure"))
	eventually.Eventually(t, "the second replica to report not ready", func() bool { return second.server.Check(nil) != nil })
	healthClient := grpc_health_v1.NewHealthClient(secondConnection)
	waitForHealthStatus(t, healthClient, grpc_health_v1.HealthCheckResponse_NOT_SERVING)
	callCtx, cancel = callContext(t)
	_, err = delegatedClient.ResumeEnvironment(callCtx, delegatedRequest)
	cancel()
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("delegated resume during policy failure returned %v, want Unavailable", err)
	}
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	if sandbox.Spec.WakeGeneration != initialGeneration+1 {
		t.Fatalf("policy failure changed wake generation to %d", sandbox.Spec.WakeGeneration)
	}
	second.server.policyHealth.RecordSuccess("policysets")
	eventually.Eventually(t, "the second replica to be ready again", func() bool { return second.server.Check(nil) == nil })
	waitForHealthStatus(t, healthClient, grpc_health_v1.HealthCheckResponse_SERVING)
	eventuallyResumeOutcome(t, delegatedClient, delegatedRequest, environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING)
	if err := objects.Delete(t.Context(), policyBinding); err != nil {
		t.Fatal(err)
	}
	eventuallyRPCCode(t, delegatedClient, delegatedRequest, codes.PermissionDenied)
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	if sandbox.Spec.WakeGeneration != initialGeneration+1 {
		t.Fatalf("revoked delegated grant changed wake generation to %d", sandbox.Spec.WakeGeneration)
	}

	var leaderIndex int
	select {
	case <-first.probe.started:
		leaderIndex = 0
	case <-second.probe.started:
		leaderIndex = 1
	case <-time.After(10 * time.Second):
		t.Fatal("neither controller-runtime manager acquired the test Lease")
	}
	if first.probe.isActive() == second.probe.isActive() {
		t.Fatalf("leader-election runnables active: first=%t second=%t", first.probe.isActive(), second.probe.isActive())
	}
	leader, standby := first, second
	standbyClient := secondClient
	if leaderIndex == 1 {
		leader, standby = second, first
		standbyClient = firstClient
	}
	watchClient := clientFor(t, ca, leader.server, peertls.CentralSamplerURI)
	watchCtx, watchCancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(watchCancel)
	watch, err := watchClient.WatchEnvironments(watchCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatalf("start leader inventory watch: %v", err)
	}
	for {
		frame, err := watch.Recv()
		if err != nil {
			t.Fatalf("read leader inventory snapshot: %v", err)
		}
		if frame.GetSnapshotComplete() != nil {
			break
		}
	}
	stopReplica(leader)
	if leader.server.proxy.health.Available() {
		t.Fatal("stopped leader retained a ready Runtime proxy inventory")
	}
	streamDone := make(chan error, 1)
	go func() {
		for {
			_, err := watch.Recv()
			if err != nil {
				streamDone <- err
				return
			}
		}
	}()
	select {
	case err := <-streamDone:
		if err == nil {
			t.Fatal("leader inventory stream remained open after bounded shutdown")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("leader inventory stream did not close after bounded shutdown")
	}
	select {
	case <-standby.probe.started:
	case <-time.After(10 * time.Second):
		t.Fatal("standby manager did not acquire the Lease after leader cancellation")
	}
	if standby.server.Check(nil) != nil {
		t.Fatal("standby listener stopped during leader takeover")
	}
	if !standby.server.proxy.health.Available() {
		t.Fatal("standby Runtime proxy inventory did not remain ready during leader takeover")
	}
	callCtx, cancel = callContext(t)
	resumed, err := standbyClient.ResumeEnvironment(callCtx, resumeRequest(target, initialGeneration+1))
	cancel()
	if err != nil || resumed.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING {
		t.Fatalf("standby resume after takeover = %v, %v", resumed, err)
	}
}

func environmentClient(t *testing.T, ca *peertlstesting.CA, server *Server) (environmentpb.EnvironmentServiceClient, *grpc.ClientConn) {
	t.Helper()
	connection := clientConnection(t, ca, server, peertls.ActionsURI)
	return environmentpb.NewEnvironmentServiceClient(connection), connection
}

func clientFor(t *testing.T, ca *peertlstesting.CA, server *Server, serviceID string) environmentpb.EnvironmentServiceClient {
	t.Helper()
	connection := clientConnection(t, ca, server, serviceID)
	t.Cleanup(func() { _ = connection.Close() })
	return environmentpb.NewEnvironmentServiceClient(connection)
}

func clientConnection(t *testing.T, ca *peertlstesting.CA, server *Server, serviceID string) *grpc.ClientConn {
	t.Helper()
	files := ca.Issue(t, serviceID)
	tlsConfig, err := files.ClientTLSConfig(peertls.SandboxControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(server.Address(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)))
	if err != nil {
		t.Fatal(err)
	}
	return connection
}

func assertRuntimeProxyAuthorization(t *testing.T, ca *peertlstesting.CA, server *Server) {
	t.Helper()
	if err := server.Check(nil); err != nil {
		t.Fatalf("EnvironmentService is not ready without a connected Runtime: %v", err)
	}
	callers := []string{
		peertls.AppURI, peertls.SessionsURI, peertls.SandboxControllerURI,
		peertls.CentralSamplerURI, peertls.ActionsURI,
	}
	connections := make(map[string]*grpc.ClientConn, len(callers))
	for _, caller := range callers {
		connections[caller] = clientConnection(t, ca, server, caller)
		connection := connections[caller]
		t.Cleanup(func() { _ = connection.Close() })
	}
	waitForHealthStatus(t, grpc_health_v1.NewHealthClient(connections[peertls.ActionsURI]), grpc_health_v1.HealthCheckResponse_SERVING)
	for _, method := range grpcmethods.Of(environmentruntimepb.EnvironmentRuntime_ServiceDesc) {
		allowed := runtimeProxyEnvironmentCaller(method.Name)
		for _, caller := range callers {
			ctx, cancel := callContext(t)
			err := method.Call(ctx, connections[caller])
			cancel()
			want := codes.PermissionDenied
			if caller == allowed && allowed != "" {
				want = codes.InvalidArgument
			}
			if got := status.Code(err); got != want {
				t.Errorf("Runtime proxy %s caller %s: %v, want %s", method.Name, caller, err, want)
			}
		}
	}
	for _, method := range grpcmethods.Of(executorpb.Executor_ServiceDesc) {
		for _, caller := range callers {
			ctx, cancel := callContext(t)
			err := method.Call(ctx, connections[caller])
			cancel()
			want := codes.PermissionDenied
			if caller == peertls.CentralSamplerURI {
				want = codes.InvalidArgument
			}
			if got := status.Code(err); got != want {
				t.Errorf("Runtime proxy %s caller %s: %v, want %s", method.Name, caller, err, want)
			}
		}
	}

	ctx, cancel := callContext(t)
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: "delegated-owner", Uid: "test-identity-uid"}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{
			RequestId: "00000000-0000-4000-8000-000000000001", ExecutionId: "00000000-0000-4000-8000-000000000002",
		}},
	})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	delegated := metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, string(encoded))
	delegatedErr := grpcmethods.Method{Name: harnesshostpb.HarnessHost_CreateSession_FullMethodName}.Call(delegated, connections[peertls.SessionsURI])
	cancel()
	if status.Code(delegatedErr) != codes.PermissionDenied {
		t.Fatalf("Runtime proxy accepted delegated service authority: %v", delegatedErr)
	}

	ctx, cancel = callContext(t)
	ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer ignored-token")
	bearerErr := grpcmethods.Method{Name: harnesshostpb.HarnessHost_ListSessions_FullMethodName}.Call(ctx, connections[peertls.AppURI])
	cancel()
	if status.Code(bearerErr) != codes.Unauthenticated {
		t.Fatalf("Runtime proxy accepted bearer metadata with an SVID: %v", bearerErr)
	}
}

func runtimeProxyEnvironmentCaller(method string) string {
	switch method {
	case environmentruntimepb.EnvironmentRuntime_GetStatus_FullMethodName:
		return peertls.AppURI
	case harnesshostpb.HarnessHost_CreateSession_FullMethodName,
		harnesshostpb.HarnessHost_SubmitInput_FullMethodName,
		harnesshostpb.HarnessHost_ChangeModel_FullMethodName,
		harnesshostpb.HarnessHost_ChangeReasoningEffort_FullMethodName,
		harnesshostpb.HarnessHost_InterruptTurn_FullMethodName:
		return peertls.SessionsURI
	case environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName,
		environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName,
		environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName:
		return peertls.SandboxControllerURI
	case environmentruntimepb.EnvironmentRuntime_WatchState_FullMethodName:
		return ""
	default:
		panic("unclassified Runtime proxy method: " + method)
	}
}

func delegatedEnvironmentClient(
	t *testing.T,
	ca *peertlstesting.CA,
	server *Server,
	identity *policyapi.Identity,
) (environmentpb.EnvironmentServiceClient, *grpc.ClientConn) {
	t.Helper()
	actorCertificate := ca.Issue(t, peertls.ActionsURI)
	transport := &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.SandboxControllerURI,
		CertFile: actorCertificate.CertFile, KeyFile: actorCertificate.KeyFile, ClientURI: peertls.ActionsURI,
	}
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{
			RequestId:   "00000000-0000-4000-8000-000000000001",
			ExecutionId: "00000000-0000-4000-8000-000000000002",
		}},
	}
	connection, err := workloadgrpc.DialDelegated(server.Address(), "", 10*time.Second, transport, workloadgrpc.ClientInterceptors{
		Unary: func(ctx context.Context, method string, request, response any, connection *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
			delegated, err := workloadgrpc.WithDelegation(ctx, envelope)
			if err != nil {
				return err
			}
			return invoke(delegated, method, request, response, connection, options...)
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return environmentpb.NewEnvironmentServiceClient(connection), connection
}

func resumeRequest(target *identitypb.EnvironmentTarget, generation int64) *environmentpb.ResumeEnvironmentRequest {
	return &environmentpb.ResumeEnvironmentRequest{
		Target: target,
		ResumeToken: lifecycle.NewResumeToken(&lifecycle.Target{
			Namespace: target.GetNamespace(), Name: target.GetName(), Uid: target.GetUid(),
		}, generation),
	}
}

func waitForHealthStatus(t *testing.T, health grpc_health_v1.HealthClient, expected grpc_health_v1.HealthCheckResponse_ServingStatus) {
	t.Helper()
	eventually.Eventually(t, "gRPC health status "+expected.String(), func() bool {
		ctx, cancel := callContext(t)
		defer cancel()
		response, err := health.Check(ctx, &grpc_health_v1.HealthCheckRequest{})
		return err == nil && response.GetStatus() == expected
	})
}

func eventuallyResumeOutcome(
	t *testing.T,
	rpc environmentpb.EnvironmentServiceClient,
	request *environmentpb.ResumeEnvironmentRequest,
	expected environmentpb.ResumeOutcome,
) {
	t.Helper()
	var lastResponse *environmentpb.ResumeEnvironmentResponse
	var lastErr error
	eventually.Eventually(t, "delegated resume outcome "+expected.String(), func() bool {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()
		lastResponse, lastErr = rpc.ResumeEnvironment(ctx, request)
		return lastErr == nil && lastResponse.GetOutcome() == expected
	}, func() string { return fmt.Sprintf("delegated resume outcome = %v, %v", lastResponse, lastErr) })
}

func eventuallyRPCCode(
	t *testing.T,
	rpc environmentpb.EnvironmentServiceClient,
	request *environmentpb.ResumeEnvironmentRequest,
	expected codes.Code,
) {
	t.Helper()
	var lastErr error
	eventually.Eventually(t, "delegated resume error code "+expected.String(), func() bool {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()
		_, lastErr = rpc.ResumeEnvironment(ctx, request)
		return status.Code(lastErr) == expected
	}, func() string { return fmt.Sprintf("delegated resume error = %v", lastErr) })
}

func callContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(t.Context(), 10*time.Second)
}

func TestRuntimeProxyRejectsBindingObserverCredentials(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	for _, uri := range []string{peertls.RuntimeProxyURI, peertls.SandboxControllerURI, peertls.GatewayURI} {
		t.Run(uri, func(t *testing.T) {
			leaf := ca.Issue(t, uri)
			c := RuntimeProxyConfig{RunnerSecurity: peertls.Security{Mode: peertls.MutualTLS, TLS: &leaf}}
			err := c.ValidateCredentials()
			if (err == nil) != (uri == peertls.RuntimeProxyURI) {
				t.Fatalf("Runtime proxy credentials %s: %v", uri, err)
			}
		})
	}
}
