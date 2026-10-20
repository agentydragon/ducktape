package gateway

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
	ktesting "k8s.io/client-go/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testNamespace  = "testing"
	testSandbox    = "sandbox-a"
	testSandboxUID = "sandbox-uid-a"
	testPod        = "runner-pod-a"
	testPodUID     = "runner-pod-uid-a"
)

type gatewayTestTokenReviews struct{}

func newSandboxClient(t *testing.T, sandbox *sandboxapi.Sandbox) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(sandbox).Build()
}

func (gatewayTestTokenReviews) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	accounts := map[string]auth.Account{
		"runner-token":     {Namespace: testNamespace, Name: testSandbox},
		"app-token":        {Namespace: "apps", Name: "app"},
		"controller-token": {Namespace: "system", Name: "sandbox-controller"},
	}
	account, ok := accounts[request.Spec.Token]
	if !ok {
		return &authenticationv1.TokenReview{}, nil
	}
	extra := map[string]authenticationv1.ExtraValue{}
	if request.Spec.Token == "runner-token" {
		extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{testPod}
		extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{testPodUID}
	} else {
		extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{"caller-pod"}
		extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{"caller-pod-uid"}
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane-test"},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + account.Namespace + ":" + account.Name,
			UID:      "service-account-uid",
			Extra:    extra,
		},
	}}, nil
}

type serviceFrameReader interface {
	Recv() (*runnercontrolpb.ServiceFrame, error)
}

func TestRunnerControlTunnelRoutesAuthenticatedProxyCalls(t *testing.T) {
	reviews := gatewayTestTokenReviews{}
	callerAuth, err := auth.NewResolver(reviews, "agentplane-test", []string{"apps", "system"})
	if err != nil {
		t.Fatal(err)
	}
	runnerAuth, err := auth.NewResolver(reviews, "agentplane-test", []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}

	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: testSandbox, Namespace: testNamespace, UID: testSandboxUID},
		Status: sandboxapi.SandboxStatus{
			OperatingMode: sandboxapi.OperatingModeRunning,
			Incarnation:   &sandboxapi.Incarnation{Name: testPod, UID: testPodUID},
			Conditions:    []metav1.Condition{{Type: sandboxapi.ConditionRunning, Status: metav1.ConditionTrue, Reason: "Ready"}},
		},
	}
	controller := true
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: testPod, Namespace: testNamespace, UID: testPodUID,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: testSandbox,
				UID: testSandboxUID, Controller: &controller,
			}},
		},
		Spec: corev1.PodSpec{ServiceAccountName: testSandbox},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{
			Type: corev1.PodReady, Status: corev1.ConditionTrue,
		}}},
	}
	objects := newSandboxClient(t, sandbox)
	kube := kubernetesfake.NewSimpleClientset(pod)
	controllerAccount := auth.Account{Namespace: "system", Name: "sandbox-controller"}
	gateway, err := New(Config{
		Namespace:                testNamespace,
		CallerAccounts:           []auth.Account{{Namespace: "apps", Name: "app"}},
		SandboxControllerAccount: &controllerAccount,
		HolderName:               "runner-gateway-0", HolderIP: "127.0.0.1", ProxyPort: sandboxapi.ControlServicePort,
		ControlTiming: ControlTiming{
			HeartbeatInterval:  time.Second,
			LivenessTimeout:    10 * time.Second,
			LeaseTouchInterval: 5 * time.Second,
			MaximumAge:         time.Minute,
		},
	}, callerAuth, runnerAuth, objects, kube.CoreV1(), kube.CoordinationV1(), kube.DiscoveryV1())
	if err != nil {
		t.Fatal(err)
	}
	view, err := gateway.sandboxes.get(context.Background(), testSandbox)
	if err != nil {
		t.Fatalf("test Sandbox fixture is not readable: %v", err)
	}
	runnerPod, err := kube.CoreV1().Pods(testNamespace).Get(context.Background(), testPod, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if view.identity.Name != testSandbox || view.identity.UID != testSandboxUID || len(runnerPod.OwnerReferences) != 1 || runnerPod.OwnerReferences[0].APIVersion != sandboxapi.GroupVersion.String() || runnerPod.OwnerReferences[0].Kind != sandboxapi.SandboxKind || runnerPod.OwnerReferences[0].UID != testSandboxUID {
		t.Fatalf("test runner ownership fixture is malformed: sandbox=%#v owner=%#v", view.identity, runnerPod.OwnerReferences)
	}
	runnerPrincipal, err := runnerAuth.Resolve(metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer runner-token")))
	if err != nil {
		t.Fatal(err)
	}
	if runnerPrincipal.Account != (auth.Account{Namespace: testNamespace, Name: testSandbox}) || runnerPrincipal.PodName != testPod || runnerPrincipal.PodUID != testPodUID {
		t.Fatalf("TokenReview returned the wrong runner principal: %#v", runnerPrincipal)
	}
	if _, _, err := gateway.sandboxes.runnerPeer(context.Background(), runnerPrincipal); err != nil {
		t.Fatalf("runner peer resolution rejected the test Sandbox Pod: %v", err)
	}
	wrongRunner := runnerPrincipal
	wrongRunner.Account.Name = "other-sandbox"
	if _, _, err := gateway.sandboxes.runnerPeer(context.Background(), wrongRunner); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("a runner token for another ServiceAccount was accepted: %v", err)
	}
	missingRunnerPodUID := runnerPrincipal
	missingRunnerPodUID.PodUID = ""
	if _, _, err := gateway.sandboxes.runnerPeer(context.Background(), missingRunnerPodUID); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("a runner principal without its Pod UID was accepted: %v", err)
	}
	wrongOwnerPod := runnerPod.DeepCopy()
	wrongOwnerPod.OwnerReferences[0].UID = types.UID("replacement-sandbox-uid")
	if _, err := kube.CoreV1().Pods(testNamespace).Update(context.Background(), wrongOwnerPod, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("update runner Pod owner for denial case: %v", err)
	}
	if _, _, err := gateway.sandboxes.runnerPeer(context.Background(), runnerPrincipal); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("a runner Pod owned by another Sandbox incarnation was accepted: %v", err)
	}
	if _, err := kube.CoreV1().Pods(testNamespace).Update(context.Background(), runnerPod, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("restore runner Pod owner after denial case: %v", err)
	}

	listener := newBufconnListener()
	server := grpc.NewServer()
	gateway.RegisterRunnerControl(server)
	gateway.RegisterProxy(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///runner-gateway-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })

	streamContext, stopStream := context.WithCancel(metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer runner-token")))
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
	if welcomeFrame.GetWelcome() == nil || welcomeFrame.GetWelcome().GetConnectionEpoch() != 1 || welcomeFrame.GetWelcome().GetIncarnationUid() != testPodUID {
		t.Fatalf("unexpected Welcome frame: %v", welcomeFrame)
	}

	lease, err := kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.PodUIDAnnotation] != testPodUID || lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != "runner-boot-a" {
		t.Fatalf("Lease does not identify the authenticated runner: %#v", lease.Annotations)
	}
	endpoints, err := kube.DiscoveryV1().EndpointSlices(testNamespace).Get(context.Background(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(endpoints.Endpoints) != 1 || endpoints.Endpoints[0].Addresses[0] != "127.0.0.1" {
		t.Fatalf("control route does not point at the connection holder: %#v", endpoints.Endpoints)
	}

	client := runnerservicepb.NewRunnerClient(connection)
	request := &runnerservicepb.OpenSessionRequest{SessionId: "session-a"}
	callDone := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(callerContext("app-token"), 5*time.Second)
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
	if err := os.WriteFile(tokenFile, []byte("app-token"), 0o600); err != nil {
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
		Namespace: testNamespace, TokenFile: tokenFile, Timeout: 5 * time.Second,
		TargetTemplate: proxyListener.Addr().String(),
	}
	deliveryDone := make(chan struct {
		entry *eventlogpb.EventEntry
		err   error
	}, 1)
	go func() {
		entry, callErr := deliveryRunner.Deliver(context.Background(), dispatcher.SandboxSnapshot{
			Name: testSandbox, Namespace: testNamespace, UID: testSandboxUID,
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
		ctx, cancel := context.WithTimeout(callerContext("app-token"), 5*time.Second)
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
		"authorization", "Bearer app-token",
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, "sandbox-uid-old",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, testNamespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	_, err = client.ListSessions(wrongIncarnation, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("stale Sandbox UID should not route to the current runner: %v", err)
	}

	wrongOwner := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer app-token",
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, testSandboxUID,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, testNamespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, "another-sandbox",
	))
	_, err = client.ListSessions(wrongOwner, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("a mismatched Sandbox owner should not route to the current runner: %v", err)
	}

	incompleteIdentity := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer app-token",
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, testNamespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
	_, err = client.ListSessions(incompleteIdentity, &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a proxy request without the Sandbox UID should be rejected: %v", err)
	}

	_, err = client.Quiesce(callerContext("app-token"), &runnerpb.QuiesceRequest{})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ordinary caller must not Quiesce the runner: %v", err)
	}

	controllerTokenFile := filepath.Join(t.TempDir(), "controller-token")
	if err := os.WriteFile(controllerTokenFile, []byte("controller-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	quiescer := sandboxcontroller.GatewayQuiescer{Target: proxyListener.Addr().String(), TokenFile: controllerTokenFile}
	destination := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Name: testSandbox, Namespace: testNamespace, UID: types.UID(testSandboxUID),
	}}
	staleDestination := destination.DeepCopy()
	staleDestination.UID = types.UID("replaced-sandbox-uid")
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
	lease, err = kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.QuiesceStageAnnotation] != "draining" {
		t.Fatalf("runner fact was not published to the Lease: %#v", lease.Annotations)
	}

	// A replacement runner stream can land on another Gateway replica. Both replicas
	// observe the same Lease; the original must stop serving proxy calls as soon as
	// the newer epoch appears, while the control Service moves to the new holder.
	// The fake tracker does not implement the API server's sendInitialEvents bookmark,
	// which client-go's informer requires before its initial cache can sync. Bridge
	// tracker events into a watch with the initial Lease and bookmark.
	kube.PrependWatchReactor("leases", func(action ktesting.Action) (bool, watch.Interface, error) {
		source, err := kube.Tracker().Watch(action.GetResource(), action.GetNamespace())
		if err != nil {
			return true, nil, err
		}
		events := make(chan watch.Event, 16)
		proxy := watch.NewProxyWatcher(events)
		go func() {
			defer source.Stop()
			defer close(events)
			send := func(event watch.Event) bool {
				select {
				case events <- event:
					return true
				case <-proxy.StopChan():
					return false
				}
			}
			initial, getErr := kube.Tracker().Get(action.GetResource(), action.GetNamespace(), testSandbox+sandboxlease.RunnerLeaseSuffix)
			if getErr == nil && !send(watch.Event{Type: watch.Added, Object: initial}) {
				return
			}
			if !send(watch.Event{Type: watch.Bookmark, Object: &coordinationv1.Lease{
				ObjectMeta: metav1.ObjectMeta{Annotations: map[string]string{"k8s.io/initial-events-end": "true"}},
			}}) {
				return
			}
			for {
				select {
				case event, ok := <-source.ResultChan():
					if !ok || !send(event) {
						return
					}
				case <-proxy.StopChan():
					return
				}
			}
		}()
		return true, proxy, nil
	})
	watchContext, stopWatching := context.WithCancel(context.Background())
	t.Cleanup(stopWatching)
	if err := gateway.WatchConnectionLeases(watchContext, kube); err != nil {
		t.Fatal(err)
	}
	other, err := New(Config{
		Namespace:                testNamespace,
		CallerAccounts:           []auth.Account{{Namespace: "apps", Name: "app"}},
		SandboxControllerAccount: &controllerAccount,
		HolderName:               "runner-gateway-1", HolderIP: "127.0.0.2", ProxyPort: sandboxapi.ControlServicePort,
		ControlTiming: ControlTiming{
			HeartbeatInterval: time.Second, LivenessTimeout: 10 * time.Second,
			LeaseTouchInterval: 5 * time.Second, MaximumAge: time.Minute,
		},
	}, callerAuth, runnerAuth, objects, kube.CoreV1(), kube.CoordinationV1(), kube.DiscoveryV1())
	if err != nil {
		t.Fatal(err)
	}
	if err := other.WatchConnectionLeases(watchContext, kube); err != nil {
		t.Fatal(err)
	}
	otherListener := newBufconnListener()
	otherServer := grpc.NewServer()
	other.RegisterRunnerControl(otherServer)
	other.RegisterProxy(otherServer)
	go func() { _ = otherServer.Serve(otherListener) }()
	t.Cleanup(otherServer.Stop)
	otherConnection, err := grpc.NewClient(
		"passthrough:///other-runner-gateway-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
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
		observed := gateway.connections.leaseEpochs[testSandboxUID]
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
	_, err = client.ListSessions(callerContext("app-token"), &runnerpb.ListSessionsRequest{})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("superseded Gateway still proxied to old runner: %v", err)
	}
	if err := gateway.records.pointControlEndpoints(context.Background(), view.identity, 1); err != nil {
		t.Fatal(err)
	}
	endpoints, err = kube.DiscoveryV1().EndpointSlices(testNamespace).Get(context.Background(), testSandbox+sandboxapi.ControlServiceSuffix, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(endpoints.Endpoints) != 1 || endpoints.Endpoints[0].Addresses[0] != "127.0.0.2" ||
		endpoints.Annotations[sandboxlease.ConnectionEpochAnnotation] != "2" {
		t.Fatalf("old epoch overwrote successor control route: %#v", endpoints)
	}
	otherClient := runnerservicepb.NewRunnerClient(otherConnection)
	otherCall := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(callerContext("app-token"), 5*time.Second)
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

func callerContext(token string) context.Context {
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+token,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, testSandboxUID,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, testNamespace,
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
	sender := &controlFrameSender{
		ctx:     context.Background(),
		timeout: 50 * time.Millisecond,
		send: func(*runnercontrolpb.ServiceFrame) error {
			started <- struct{}{}
			<-release
			returned <- struct{}{}
			return nil
		},
	}

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

func TestGeneratedRunnerClientPreservesTunnelStatusTrailersAndCancellation(t *testing.T) {
	frames := make(chan *runnercontrolpb.ServiceFrame, 2)
	connection := newRunnerConnection(sandboxreader.Identity{}, "pod-uid", "boot-id", 1, func(frame *runnercontrolpb.ServiceFrame) error {
		frames <- frame
		return nil
	})
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
