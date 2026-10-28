package gateway

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	tlstest "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience = "test-gateway"
	testSandbox  = "test-sandbox-a"
	testPod      = "test-runner-pod-a"
)

var (
	// api is the API server the binary's tests share; each test's Sandbox is in a namespace of its own.
	api        *kubeapiserver.Server
	namespaces atomic.Int32

	appAccount        = auth.Account{Namespace: "test-apps", Name: "test-app"}
	executorAccount   = auth.Account{Namespace: "test-samplers", Name: "test-sampler"}
	controllerAccount = auth.Account{Namespace: "test-system", Name: "test-sandbox-controller"}
)

func useAPIServer(server *kubeapiserver.Server) error {
	api = server
	return nil
}

// sandboxFixture is a Running Sandbox whose runner Pod is Ready, in a namespace of the test's own, with
// tokens for that runner and for the Gateway's callers, and resolvers that review them.
type sandboxFixture struct {
	namespace                                             string
	sandbox                                               *sandboxapi.Sandbox
	pod                                                   *corev1.Pod
	objects                                               client.Client
	runnerToken, appToken, executorToken, controllerToken string
	callerAuth                                            *auth.Resolver
	ca                                                    *tlstest.CA
	gatewayTLS, runnerTLS                                 runner_transport.Config
}

func newSandboxFixture(t *testing.T) *sandboxFixture {
	t.Helper()
	ctx := t.Context()
	f := &sandboxFixture{namespace: fmt.Sprintf("test-sandboxes-%d", namespaces.Add(1))}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, sandboxapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	var err error
	if f.objects, err = client.New(api.Config, client.Options{Scheme: scheme}); err != nil {
		t.Fatal(err)
	}
	if _, err := api.ServiceAccount(ctx, auth.Account{Namespace: f.namespace, Name: testSandbox}); err != nil {
		t.Fatal(err)
	}
	f.sandbox = &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: testSandbox, Namespace: f.namespace},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template"},
	}
	if err := f.objects.Create(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	controller := true
	pods := api.Clientset.CoreV1().Pods(f.namespace)
	f.pod, err = pods.Create(ctx, &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: testPod, Namespace: f.namespace,
			Labels: map[string]string{sandboxapi.RunnerTransportLabel: "reverse", sandboxapi.RunnerCredentialLabel: "test-runner-credential"},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: testSandbox,
				UID: f.sandbox.UID, Controller: &controller,
			}},
		},
		Spec: corev1.PodSpec{
			ServiceAccountName: testSandbox,
			Containers:         []corev1.Container{{Name: "runner", Image: "registry.test/unused"}},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	f.pod.Status = corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "192.0.2.20", Conditions: []corev1.PodCondition{{
		Type: corev1.PodReady, Status: corev1.ConditionTrue,
	}}}
	if f.pod, err = pods.UpdateStatus(ctx, f.pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	f.sandbox.Status = sandboxapi.SandboxStatus{
		OperatingMode: sandboxapi.OperatingModeRunning,
		Incarnation:   &sandboxapi.Incarnation{Name: f.pod.Name, UID: string(f.pod.UID)},
		Conditions: []metav1.Condition{{
			Type: sandboxapi.ConditionRunning, Status: metav1.ConditionTrue, Reason: "Ready", LastTransitionTime: metav1.Now(),
		}},
	}
	if err := f.objects.Status().Update(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	if f.runnerToken, err = api.PodToken(ctx, f.pod, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.appToken, err = api.WorkloadToken(ctx, appAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.executorToken, err = api.WorkloadToken(ctx, executorAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.controllerToken, err = api.WorkloadToken(ctx, controllerAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.callerAuth, err = auth.NewResolver(api.TokenReviews(), testAudience,
		[]string{appAccount.Namespace, controllerAccount.Namespace, executorAccount.Namespace}); err != nil {
		t.Fatal(err)
	}
	f.ca = tlstest.NewCA(t)
	f.gatewayTLS = f.ca.Issue(t, runner_transport.GatewayURI)
	uri, err := runner_transport.RunnerURI(f.namespace, string(f.sandbox.UID), f.pod.Labels[sandboxapi.RunnerCredentialLabel])
	if err != nil {
		t.Fatal(err)
	}
	f.runnerTLS = f.ca.Issue(t, uri)
	return f
}

// newGateway is a Gateway replica serving the fixture's namespace that names itself holderName at
// holderIP in the control routes it writes.
func (f *sandboxFixture) newGateway(t *testing.T, holderName, holderIP string, executorCallers ...auth.Account) *Gateway {
	t.Helper()
	gateway, err := New(Config{
		Namespace:        f.namespace,
		RunnerTransports: []string{"forward", "reverse"}, RunnerTLS: f.gatewayTLS,
		CallerAccounts:           []auth.Account{appAccount},
		ExecutorCallers:          executorCallers,
		SandboxControllerAccount: &controllerAccount,
		HolderName:               holderName, HolderIP: holderIP, ProxyPort: sandboxapi.ControlServicePort,
		ControlTiming: ControlTiming{
			HeartbeatInterval:  time.Second,
			LivenessTimeout:    10 * time.Second,
			LeaseTouchInterval: 5 * time.Second,
			MaximumAge:         time.Minute,
		},
	}, f.callerAuth, f.objects, api.Clientset.CoreV1(), api.Clientset.CoordinationV1(), api.Clientset.DiscoveryV1())
	if err != nil {
		t.Fatal(err)
	}
	return gateway
}

type serviceFrameReader interface {
	Recv() (*runnercontrolpb.ServiceFrame, error)
}

func TestRunnerControlTunnelRoutesAuthenticatedProxyCalls(t *testing.T) {
	f := newSandboxFixture(t)
	sandboxUID, podUID := string(f.sandbox.UID), string(f.pod.UID)
	// Each replica is reached on a listener of its own; its holder address is only what the control
	// route it writes names.
	gateway := f.newGateway(t, "test-runner-gateway-0", "192.0.2.10")
	view, err := gateway.sandboxes.get(context.Background(), testSandbox)
	if err != nil {
		t.Fatalf("test Sandbox fixture is not readable: %v", err)
	}
	uri, err := f.runnerTLS.LocalIdentity()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := gateway.certificatePeer(t.Context(), uri, "reverse"); err != nil {
		t.Fatal(err)
	}
	wrongURI, err := runner_transport.RunnerURI(f.namespace, "test-other-sandbox-uid", podUID)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := gateway.certificatePeer(t.Context(), wrongURI, "reverse"); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("another Sandbox certificate was accepted: %v", err)
	}
	setOwnerUID := func(uid types.UID) {
		t.Helper()
		pods := api.Clientset.CoreV1().Pods(f.namespace)
		pod, err := pods.Get(context.Background(), testPod, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		pod.OwnerReferences[0].UID = uid
		if _, err := pods.Update(context.Background(), pod, metav1.UpdateOptions{}); err != nil {
			t.Fatalf("set the runner Pod's owner UID: %v", err)
		}
	}
	setOwnerUID("test-replacement-sandbox-uid")
	if _, _, err := gateway.certificatePeer(context.Background(), uri, "reverse"); status.Code(err) != codes.Unavailable {
		t.Fatalf("a foreign runner Pod was not rejected as unavailable: %v", err)
	}
	setOwnerUID(f.sandbox.UID)

	listener := newBufconnListener()
	server := grpc.NewServer(grpc.Creds(f.serverCredentials(t)))
	gateway.RegisterRunnerControl(server)
	gateway.RegisterProxy(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///runner-gateway-test",
		grpc.WithTransportCredentials(f.clientCredentials(t)),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })

	streamContext, stopStream := context.WithCancel(metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+f.runnerToken)))
	t.Cleanup(stopStream)
	runnerStream, err := runnercontrolpb.NewRunnerControlClient(connection).Connect(streamContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Hello{
		Hello: &runnercontrolpb.Hello{RunnerBootId: "runner-boot-a"},
	}}); err != nil {
		t.Fatal(err)
	}
	welcomeFrame, err := runnerStream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if welcomeFrame.GetWelcome() == nil || welcomeFrame.GetWelcome().GetConnectionEpoch() != 1 || welcomeFrame.GetWelcome().GetIncarnationUid() != podUID {
		t.Fatalf("unexpected Welcome frame: %v", welcomeFrame)
	}

	lease, err := api.Clientset.CoordinationV1().Leases(f.namespace).Get(context.Background(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.PodUIDAnnotation] != podUID || lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != "runner-boot-a" {
		t.Fatalf("Lease does not identify the authenticated runner: %#v", lease.Annotations)
	}
	endpoints, err := api.Clientset.DiscoveryV1().EndpointSlices(f.namespace).Get(context.Background(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(endpoints.Endpoints) != 1 || endpoints.Endpoints[0].Addresses[0] != "192.0.2.10" {
		t.Fatalf("control route does not point at the connection holder: %#v", endpoints.Endpoints)
	}

	client := runnerservicepb.NewRunnerClient(connection)
	request := &runnerservicepb.OpenSessionRequest{SessionId: "session-a"}
	callDone := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(f.callerContext(f.appToken), 5*time.Second)
		defer cancel()
		response, callErr := client.OpenSession(ctx, request)
		if callErr == nil && response.GetAttached().GetSessionId() != "session-a" {
			callErr = status.Error(codes.Internal, "unexpected attached Session")
		}
		callDone <- callErr
	}()

	call := receiveTunnelCall(t, runnerStream)
	if call.GetMethod() != "/agentplane.runner.Runner/OpenSession" {
		t.Fatalf("unexpected tunneled method: %q", call.GetMethod())
	}
	if call.GetTimeout() == nil || call.GetTimeout().AsDuration() <= 0 {
		t.Fatalf("caller deadline was not forwarded: %v", call.GetTimeout())
	}
	var opened runnerservicepb.OpenSessionRequest
	if err := proto.Unmarshal(call.GetRequest(), &opened); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(&opened, request) {
		t.Fatalf("runner request changed across the tunnel: got %v want %v", &opened, request)
	}
	attached, err := proto.Marshal(&runnerservicepb.OpenSessionResponse{Attached: &runnerpb.Attached{SessionId: "session-a"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: call.GetCallId(), Response: attached},
	}}); err != nil {
		t.Fatal(err)
	}
	if err := <-callDone; err != nil {
		t.Fatal(err)
	}

	// Exercise the actual Dispatcher Runner client through the Gateway proxy and
	// runner tunnel. It carries the full destination fence and returns the
	// runner's admission rather than a proxy receipt.
	tokenFile := filepath.Join(t.TempDir(), "gateway-token")
	if err := os.WriteFile(tokenFile, []byte(f.appToken), 0o600); err != nil {
		t.Fatal(err)
	}
	proxyListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxyServer := grpc.NewServer()
	gateway.RegisterProxy(proxyServer)
	go func() { _ = proxyServer.Serve(proxyListener) }()
	t.Cleanup(proxyServer.Stop)
	t.Cleanup(func() { _ = proxyListener.Close() })
	deliveryRunner := &dispatcher.ProxyRunner{
		Namespace: f.namespace, TokenFile: tokenFile, Timeout: 5 * time.Second,
		TargetTemplate: proxyListener.Addr().String(),
	}
	deliveryDone := make(chan struct {
		entry *eventlogpb.EventEntry
		err   error
	}, 1)
	go func() {
		entry, callErr := deliveryRunner.Deliver(context.Background(), dispatcher.SandboxSnapshot{
			Name: testSandbox, Namespace: f.namespace, UID: sandboxUID,
		}, "session-a", &commandpb.Command{CommandId: "command-a", Operation: &commandpb.Command_SubmitInput{
			SubmitInput: &commandpb.SubmitInput{Text: "hello"},
		}})
		deliveryDone <- struct {
			entry *eventlogpb.EventEntry
			err   error
		}{entry, callErr}
	}()
	inputCall := receiveTunnelCall(t, runnerStream)
	var inputRequest runnerservicepb.SubmitInputRequest
	if inputCall.GetMethod() != "/agentplane.runner.Runner/SubmitInput" ||
		proto.Unmarshal(inputCall.GetRequest(), &inputRequest) != nil ||
		inputRequest.GetSessionId() != "session-a" || inputRequest.GetCommandId() != "command-a" || inputRequest.GetText() != "hello" {
		t.Fatalf("Dispatcher input changed across Gateway tunnel: call=%v request=%v", inputCall, &inputRequest)
	}
	inputResponse, err := proto.Marshal(&runnerservicepb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 42}})
	if err != nil {
		t.Fatal(err)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: inputCall.GetCallId(), Response: inputResponse},
	}}); err != nil {
		t.Fatal(err)
	}
	select {
	case delivered := <-deliveryDone:
		if delivered.err != nil || delivered.entry.GetCursor() != 42 {
			t.Fatalf("Dispatcher did not receive runner admission: %+v", delivered)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Dispatcher call did not complete through Gateway tunnel")
	}

	var trailers metadata.MD
	statusDone := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(f.callerContext(f.appToken), 5*time.Second)
		defer cancel()
		_, callErr := client.GetStatus(ctx, &runnerpb.GetStatusRequest{}, grpc.Trailer(&trailers))
		statusDone <- callErr
	}()
	statusCall := receiveTunnelCall(t, runnerStream)
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{
			CallId: statusCall.GetCallId(), Code: int32(codes.NotFound), Details: "status unavailable",
			TrailingMetadata: []*tunnelpb.Metadata{{Key: "runner-trace", Value: &tunnelpb.Metadata_Text{Text: "trace-a"}}},
		},
	}}); err != nil {
		t.Fatal(err)
	}
	if statusCall.GetMethod() != "/agentplane.runner.Runner/GetStatus" {
		t.Fatalf("runner status method did not survive the proxy: %v", statusCall)
	}
	if err := <-statusDone; status.Code(err) != codes.NotFound {
		t.Fatalf("runner status did not survive the tunnel: %v", err)
	}
	if got := trailers.Get("runner-trace"); len(got) != 1 || got[0] != "trace-a" {
		t.Fatalf("runner trailing metadata did not survive the tunnel: %#v", trailers)
	}

	wrongIncarnation := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+f.appToken,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, "test-sandbox-uid-old",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	_, err = client.ListSessions(wrongIncarnation, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("stale Sandbox UID should not route to the current runner: %v", err)
	}

	wrongOwner := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+f.appToken,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, sandboxUID,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, "test-another-sandbox",
	))
	_, err = client.ListSessions(wrongOwner, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("a mismatched Sandbox owner should not route to the current runner: %v", err)
	}

	incompleteIdentity := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+f.appToken,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	_, err = client.ListSessions(incompleteIdentity, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a proxy request without the Sandbox UID should be rejected: %v", err)
	}

	_, err = client.Quiesce(f.callerContext(f.appToken), &runnerpb.QuiesceRequest{})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ordinary caller must not Quiesce the runner: %v", err)
	}

	controllerTokenFile := filepath.Join(t.TempDir(), "controller-token")
	if err := os.WriteFile(controllerTokenFile, []byte(f.controllerToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	quiescer := sandboxcontroller.GatewayQuiescer{Target: proxyListener.Addr().String(), TokenFile: controllerTokenFile}
	destination := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Name: testSandbox, Namespace: f.namespace, UID: f.sandbox.UID,
	}}
	staleDestination := destination.DeepCopy()
	staleDestination.UID = "test-replaced-sandbox-uid"
	staleCtx, cancelStale := context.WithTimeout(context.Background(), 5*time.Second)
	_, staleErr := quiescer.Quiesce(staleCtx, staleDestination, nil)
	cancelStale()
	if status.Code(staleErr) != codes.NotFound {
		t.Fatalf("controller Quiesce accepted a replaced Sandbox: %v", staleErr)
	}

	controllerCall := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		result, callErr := quiescer.Quiesce(ctx, destination, nil)
		if callErr == nil && result.Quiescing {
			callErr = status.Error(codes.Internal, "controller did not observe the runner's completed Quiesce")
		}
		controllerCall <- callErr
	}()
	quiesceCall := receiveTunnelCall(t, runnerStream)
	if quiesceCall.GetMethod() != "/agentplane.runner.Runner/Quiesce" {
		t.Fatalf("controller call did not reach the runner's Quiesce method: %v", quiesceCall)
	}
	quiesced, err := proto.Marshal(&runnerpb.QuiesceResponse{Stage: runnerpb.QuiesceStage_QUIESCE_STAGE_QUIESCED})
	if err != nil {
		t.Fatal(err)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: quiesceCall.GetCallId(), Response: quiesced},
	}}); err != nil {
		t.Fatal(err)
	}
	if err := <-controllerCall; err != nil {
		t.Fatal(err)
	}

	// A conditional Quiesce reaches the runner with the idle-since token the controller observed.
	idleSince := time.Date(2026, 10, 17, 3, 0, 0, 123456000, time.UTC)
	conditionalCall := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, callErr := quiescer.Quiesce(ctx, destination, &idleSince)
		conditionalCall <- callErr
	}()
	conditional := receiveTunnelCall(t, runnerStream)
	var conditionalRequest runnerpb.QuiesceRequest
	if err := proto.Unmarshal(conditional.GetRequest(), &conditionalRequest); err != nil {
		t.Fatal(err)
	}
	if !conditionalRequest.GetIfIdleSince().AsTime().Equal(idleSince) {
		t.Fatalf("the runner received if_idle_since %v, want %v", conditionalRequest.GetIfIdleSince().AsTime(), idleSince)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: conditional.GetCallId(), Response: quiesced},
	}}); err != nil {
		t.Fatal(err)
	}
	if err := <-conditionalCall; err != nil {
		t.Fatal(err)
	}

	facts := &runnercontrolpb.RunnerFacts{QuiesceStage: runnercontrolpb.RunnerFacts_QUIESCE_STAGE_DRAINING}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Heartbeat{
		Heartbeat: &runnercontrolpb.RunnerHeartbeat{Facts: facts},
	}}); err != nil {
		t.Fatal(err)
	}
	for {
		frame, recvErr := runnerStream.Recv()
		if recvErr != nil {
			t.Fatal(recvErr)
		}
		if frame.GetHeartbeat() == nil {
			continue
		}
		break
	}
	lease, err = api.Clientset.CoordinationV1().Leases(f.namespace).Get(context.Background(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.QuiesceStageAnnotation] != "draining" {
		t.Fatalf("runner fact was not published to the Lease: %#v", lease.Annotations)
	}

	// A replacement runner stream can land on another Gateway replica. Both replicas
	// observe the same Lease; the original must stop serving proxy calls as soon as
	// the newer epoch appears, while the control Service moves to the new holder.
	watchContext, stopWatching := context.WithCancel(context.Background())
	t.Cleanup(stopWatching)
	if err := gateway.WatchConnectionLeases(watchContext, api.Clientset); err != nil {
		t.Fatal(err)
	}
	other := f.newGateway(t, "test-runner-gateway-1", "192.0.2.11")
	if err := other.WatchConnectionLeases(watchContext, api.Clientset); err != nil {
		t.Fatal(err)
	}
	otherListener := newBufconnListener()
	otherServer := grpc.NewServer(grpc.Creds(f.serverCredentials(t)))
	other.RegisterRunnerControl(otherServer)
	other.RegisterProxy(otherServer)
	go func() { _ = otherServer.Serve(otherListener) }()
	t.Cleanup(otherServer.Stop)
	otherConnection, err := grpc.NewClient(
		"passthrough:///other-runner-gateway-test",
		grpc.WithTransportCredentials(f.clientCredentials(t)),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return otherListener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = otherConnection.Close() })
	otherStream, err := runnercontrolpb.NewRunnerControlClient(otherConnection).Connect(streamContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := otherStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Hello{
		Hello: &runnercontrolpb.Hello{RunnerBootId: "runner-boot-b"},
	}}); err != nil {
		t.Fatal(err)
	}
	otherWelcome, err := otherStream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if otherWelcome.GetWelcome().GetConnectionEpoch() != 2 {
		t.Fatalf("replacement runner did not advance the epoch: %v", otherWelcome)
	}
	deadline := time.After(5 * time.Second)
	for {
		gateway.connections.mu.RLock()
		observed := gateway.connections.leaseEpochs[sandboxUID]
		gateway.connections.mu.RUnlock()
		if observed >= 2 {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("original Gateway did not observe successor Lease epoch; observed %d", observed)
		case <-time.After(10 * time.Millisecond):
		}
	}
	_, err = client.ListSessions(f.callerContext(f.appToken), &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("superseded Gateway still proxied to old runner: %v", err)
	}
	if err := gateway.records.pointControlEndpoints(context.Background(), view.identity, 1); err != nil {
		t.Fatal(err)
	}
	endpoints, err = api.Clientset.DiscoveryV1().EndpointSlices(f.namespace).Get(context.Background(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(endpoints.Endpoints) != 1 || endpoints.Endpoints[0].Addresses[0] != "192.0.2.11" ||
		endpoints.Annotations[sandboxlease.ConnectionEpochAnnotation] != "2" {
		t.Fatalf("old epoch overwrote successor control route: %#v", endpoints)
	}
	otherClient := runnerservicepb.NewRunnerClient(otherConnection)
	otherCall := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(f.callerContext(f.appToken), 5*time.Second)
		defer cancel()
		_, callErr := otherClient.ListSessions(ctx, &runnerpb.ListSessionsRequest{})
		otherCall <- callErr
	}()
	forwarded := receiveTunnelCall(t, otherStream)
	response, err := proto.Marshal(&runnerpb.ListSessionsResponse{})
	if err != nil {
		t.Fatal(err)
	}
	if err := otherStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: forwarded.GetCallId(), Response: response},
	}}); err != nil {
		t.Fatal(err)
	}
	if err := <-otherCall; err != nil {
		t.Fatalf("successor Gateway did not serve the runner: %v", err)
	}
}

// callerContext is a call bearing token to the fixture's Sandbox.
func (f *sandboxFixture) callerContext(token string) context.Context {
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+token,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, string(f.sandbox.UID),
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
}

func receiveTunnelCall(t *testing.T, stream serviceFrameReader) *tunnelpb.Call {
	t.Helper()
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if call := frame.GetCall(); call != nil {
			return call
		}
	}
}

func newBufconnListener() *bufconn.Listener { return bufconn.Listen(1 << 20) }

func TestControlFrameSenderRejectsWritesAfterTimeout(t *testing.T) {
	started := make(chan struct{}, 2)
	returned := make(chan struct{}, 1)
	release := make(chan struct{})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	sender := newControlFrameSender(ctx, 50*time.Millisecond, func(*runnercontrolpb.ServiceFrame) error {
		started <- struct{}{}
		<-release
		returned <- struct{}{}
		return nil
	})

	firstDone := make(chan error, 1)
	go func() {
		firstDone <- sender.Send(&runnercontrolpb.ServiceFrame{})
	}()
	<-started
	if err := <-firstDone; status.Code(err) != codes.Unavailable {
		t.Fatalf("blocked stream write did not time out: %v", err)
	}
	if err := sender.Send(&runnercontrolpb.ServiceFrame{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("sender did not preserve its terminal error: %v", err)
	}
	select {
	case <-started:
		t.Fatal("started another stream write after the first write timed out")
	default:
	}

	close(release)
	<-returned
}

func TestControlFrameSenderPreservesTerminalErrors(t *testing.T) {
	for _, ended := range []bool{false, true} {
		t.Run(fmt.Sprintf("context_ended_%t", ended), func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			want := codes.Unavailable
			if ended {
				cancel()
				want = codes.Canceled
			}
			var calls atomic.Int32
			sender := newControlFrameSender(ctx, time.Second, func(*runnercontrolpb.ServiceFrame) error {
				calls.Add(1)
				return context.Canceled
			})
			first := sender.Send(&runnercontrolpb.ServiceFrame{})
			if status.Code(first) != want {
				t.Fatalf("Send code = %v, want %v", status.Code(first), want)
			}
			if next := sender.Send(&runnercontrolpb.ServiceFrame{}); next != first {
				t.Fatalf("later Send error = %v, want original terminal error %v", next, first)
			}
			wantCalls := int32(1)
			if ended {
				wantCalls = 0
			}
			if calls.Load() != wantCalls {
				t.Fatalf("Send calls = %d, want %d", calls.Load(), wantCalls)
			}
		})
	}
}

func TestGeneratedRunnerClientPreservesTunnelStatusTrailersAndCancellation(t *testing.T) {
	frames := make(chan *runnercontrolpb.ServiceFrame, 2)
	sendFrame := func(frame *runnercontrolpb.ServiceFrame) error {
		frames <- frame
		return nil
	}
	connection := newRunnerConnection(sandboxreader.Identity{}, "pod-uid", "boot-id", 1, sendFrame, sendFrame)
	nextCall := func() *tunnelpb.Call {
		t.Helper()
		select {
		case frame := <-frames:
			return frame.GetCall()
		case <-time.After(time.Second):
			t.Fatal("generated Runner call did not reach tunnel")
			return nil
		}
	}
	var trailers metadata.MD
	statusDone := make(chan error, 1)
	go func() {
		_, err := connection.runner.GetStatus(context.Background(),
			&runnerpb.GetStatusRequest{}, grpc.Trailer(&trailers))
		statusDone <- err
	}()
	statusCall := nextCall()
	if statusCall.GetMethod() != "/agentplane.runner.Runner/GetStatus" {
		t.Fatalf("generated GetStatus method did not reach tunnel: %v", statusCall)
	}
	connection.tunnel.answer(&tunnelpb.Reply{
		CallId: statusCall.GetCallId(), Code: int32(codes.NotFound), Details: "status unavailable",
		TrailingMetadata: []*tunnelpb.Metadata{{Key: "runner-trace", Value: &tunnelpb.Metadata_Text{Text: "trace-a"}}},
	})
	if err := <-statusDone; status.Code(err) != codes.NotFound {
		t.Fatalf("generated client lost runner status: %v", err)
	}
	if got := trailers.Get("runner-trace"); len(got) != 1 || got[0] != "trace-a" {
		t.Fatalf("generated client lost runner trailers: %#v", trailers)
	}

	ctx, cancel := context.WithCancel(context.Background())
	changeDone := make(chan error, 1)
	go func() {
		_, err := connection.runner.ChangeModel(ctx, &runnerservicepb.ChangeModelRequest{})
		changeDone <- err
	}()
	changeCall := nextCall()
	if changeCall.GetMethod() != "/agentplane.runner.Runner/ChangeModel" {
		t.Fatalf("generated Runner method did not reach tunnel: %v", changeCall)
	}
	cancel()
	select {
	case err := <-changeDone:
		if status.Code(err) != codes.Canceled {
			t.Fatalf("generated client lost caller cancellation: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled Runner call remained blocked in tunnel")
	}
}

func TestExecutorGatewayRoutesBoundedMessageFrames(t *testing.T) {
	f := newSandboxFixture(t)
	gateway := f.newGateway(t, "test-runner-gateway-0", "192.0.2.10", executorAccount)
	listener := newBufconnListener()
	server := grpc.NewServer(grpc.Creds(f.serverCredentials(t)))
	gateway.RegisterRunnerControl(server)
	gateway.RegisterExecutorGateway(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///executor-gateway-test",
		grpc.WithTransportCredentials(f.clientCredentials(t)),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })

	executorClient := executorpb.NewExecutorGatewayClient(connection)
	callerContext, stopCaller := context.WithTimeout(t.Context(), 10*time.Second)
	defer stopCaller()
	unauthorizedContext := metadata.NewOutgoingContext(callerContext, metadata.Pairs("authorization", "Bearer "+f.appToken))
	unauthorized, err := executorClient.Connect(unauthorizedContext)
	if err == nil {
		_, err = unauthorized.Recv()
	}
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("executor Gateway accepted a caller outside its independent allowlist: %v", err)
	}

	destination := &sandboxespb.SandboxDestination{
		Owner:      &sandboxespb.ServiceAccount{Namespace: f.namespace, Name: testSandbox},
		Sandbox:    testSandbox,
		SandboxUid: string(f.sandbox.UID),
	}
	staleContext := metadata.NewOutgoingContext(callerContext, metadata.Pairs("authorization", "Bearer "+f.executorToken))
	stale, err := executorClient.Connect(staleContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := stale.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{
		Destination: destination, ExpectedIncarnationUid: "stale-pod-uid", SessionId: "session-a",
	}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := stale.Recv(); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("executor Gateway did not fence a stale incarnation: %v", err)
	}

	runnerContext := metadata.NewOutgoingContext(callerContext, metadata.Pairs("authorization", "Bearer "+f.runnerToken))
	runnerStream, err := runnercontrolpb.NewRunnerControlClient(connection).Connect(runnerContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Hello{
		Hello: &runnercontrolpb.Hello{RunnerBootId: "executor-runner-boot"},
	}}); err != nil {
		t.Fatal(err)
	}
	welcome, err := runnerStream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if welcome.GetWelcome() == nil || welcome.GetWelcome().GetConnectionEpoch() == 0 || welcome.GetWelcome().GetIncarnationUid() != string(f.pod.UID) {
		t.Fatalf("runner control stream returned an unexpected Welcome: %v", welcome)
	}

	publicContext := metadata.NewOutgoingContext(callerContext, metadata.Pairs("authorization", "Bearer "+f.executorToken))
	public, err := executorClient.Connect(publicContext)
	if err != nil {
		t.Fatal(err)
	}
	if err := public.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{
		Destination: destination, ExpectedIncarnationUid: string(f.pod.UID), SessionId: "session-a",
	}}}); err != nil {
		t.Fatal(err)
	}
	open := receiveRunnerExecutorFrame(t, runnerStream)
	if open.GetOpen() == nil || open.GetChannelId() == "" || open.GetOpen().GetSessionId() != "session-a" {
		t.Fatalf("runner did not receive the attributed executor open: %v", open)
	}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
		ChannelId: open.GetChannelId(),
		Frame:     &executorpb.ExecutorFrame_Ready{Ready: &executorpb.ExecutorReady{Accepted: true}},
	}}}); err != nil {
		t.Fatal(err)
	}
	ready, err := public.Recv()
	if err != nil || !ready.GetReady().GetAccepted() {
		t.Fatalf("executor Gateway did not confirm Runner readiness: frame=%v err=%v", ready, err)
	}

	textMessage := []byte(`{"op":"shell","command":"pwd"}`)
	if err := public.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
		Opcode: executorpb.ExecutorMessage_OPCODE_TEXT, Payload: textMessage,
	}}}); err != nil {
		t.Fatal(err)
	}
	toRunner := receiveRunnerExecutorFrame(t, runnerStream)
	if toRunner.GetChannelId() != open.GetChannelId() || toRunner.GetMessage().GetOpcode() != executorpb.ExecutorMessage_OPCODE_TEXT || string(toRunner.GetMessage().GetPayload()) != string(textMessage) {
		t.Fatalf("executor message changed on its way to the Runner: %v", toRunner)
	}

	binaryMessage := []byte{0, 1, 2, 0xff}
	if err := runnerStream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Executor{Executor: &executorpb.ExecutorFrame{
		ChannelId: open.GetChannelId(),
		Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
			Opcode: executorpb.ExecutorMessage_OPCODE_BINARY, Payload: binaryMessage,
		}},
	}}}); err != nil {
		t.Fatal(err)
	}
	toCaller, err := public.Recv()
	if err != nil || toCaller.GetMessage().GetOpcode() != executorpb.ExecutorMessage_OPCODE_BINARY || string(toCaller.GetMessage().GetPayload()) != string(binaryMessage) {
		t.Fatalf("binary executor message changed on its way to the caller: frame=%v err=%v", toCaller, err)
	}

	finalMessage := []byte(`{"op":"last-message-before-close"}`)
	if err := public.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
		Opcode: executorpb.ExecutorMessage_OPCODE_TEXT, Payload: finalMessage,
	}}}); err != nil {
		t.Fatal(err)
	}
	closeFrame := &executorpb.ExecutorClose{Code: 1000, Reason: "done"}
	if err := public.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Close{Close: closeFrame}}); err != nil {
		t.Fatal(err)
	}
	toRunner = receiveRunnerExecutorFrame(t, runnerStream)
	if toRunner.GetMessage() == nil || string(toRunner.GetMessage().GetPayload()) != string(finalMessage) {
		t.Fatalf("executor close overtook the preceding caller message: %v", toRunner)
	}
	toRunner = receiveRunnerExecutorFrame(t, runnerStream)
	if toRunner.GetClose().GetCode() != closeFrame.GetCode() || toRunner.GetClose().GetReason() != closeFrame.GetReason() {
		t.Fatalf("executor close frame changed on its way to the Runner: %v", toRunner)
	}
	toCaller, err = public.Recv()
	if err != nil || toCaller.GetClose().GetCode() != closeFrame.GetCode() || toCaller.GetClose().GetReason() != closeFrame.GetReason() {
		t.Fatalf("executor close frame changed on its way to the caller: frame=%v err=%v", toCaller, err)
	}
}

func receiveRunnerExecutorFrame(t *testing.T, stream interface {
	Recv() (*runnercontrolpb.ServiceFrame, error)
},
) *executorpb.ExecutorFrame {
	t.Helper()
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if executor := frame.GetExecutor(); executor != nil {
			return executor
		}
	}
}

func TestExecutorGatewayIsAbsentWithoutIndependentAllowlist(t *testing.T) {
	f := newSandboxFixture(t)
	gateway := f.newGateway(t, "test-runner-gateway-0", "192.0.2.10")
	listener := newBufconnListener()
	server := grpc.NewServer(grpc.Creds(f.serverCredentials(t)))
	gateway.RegisterExecutorGateway(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///disabled-executor-gateway-test",
		grpc.WithTransportCredentials(f.clientCredentials(t)),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })

	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Bearer "+f.executorToken))
	stream, err := executorpb.NewExecutorGatewayClient(connection).Connect(ctx)
	if err == nil {
		_, err = stream.Recv()
	}
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("executor Gateway RPC was registered without its opt-in caller allowlist: %v", err)
	}
}

func (f *sandboxFixture) serverCredentials(t *testing.T) credentials.TransportCredentials {
	t.Helper()
	c, err := f.gatewayTLS.ServerCredentials("")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func (f *sandboxFixture) clientCredentials(t *testing.T) credentials.TransportCredentials {
	t.Helper()
	c, err := f.runnerTLS.ClientCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	return c
}
