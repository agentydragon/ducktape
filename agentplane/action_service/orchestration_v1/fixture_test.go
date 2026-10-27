package orchestrationv1

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testGroup         = "test-orchestration"
	testDefaultPolicy = "test-default-policy"
	testExtraPolicy   = "test-extra-policy"
	testPresets       = `{"test-preset": {
		"title": "Test preset",
		"launch": {"template": "test-template", "policy_sets": ["test-extra-policy"]},
		"open_session": {"spec": {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/test/{session_id}"}}
	}}`
)

var (
	server     *kubeapiserver.Server
	kube       client.Client
	namespaces atomic.Int32
)

// TestMain runs one API server for every test; each test works in a namespace of its own. No controller runs, so a
// launched Sandbox never starts and nothing is garbage-collected.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(started *kubeapiserver.Server) error {
		server = started
		scheme := runtime.NewScheme()
		for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme} {
			if err := add(scheme); err != nil {
				return err
			}
		}
		var err error
		kube, err = client.New(started.Config, client.Options{Scheme: scheme})
		return err
	}))
}

// fixture is a namespace holding the default and an extra PolicySet, an Executor launching there, and the Session
// Dispatcher it calls.
type fixture struct {
	namespace  string
	caller     store.ServiceAccount
	dispatcher *recordingDispatcher
	executor   *Executor
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	namespace := fmt.Sprintf("orchestration-test-%d", namespaces.Add(1))
	ctx := t.Context()
	if _, err := server.Clientset.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{testDefaultPolicy, testExtraPolicy} {
		set := &policyapi.PolicySet{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
				Type: "egress", Hosts: []string{"test.invalid"},
			}}},
		}
		if err := kube.Create(ctx, set); err != nil {
			t.Fatal(err)
		}
	}
	dispatcher := &recordingDispatcher{}
	executor, err := NewExecutor(testGroup, catalog.OrchestrationBinding{
		Description: "test orchestration", Namespace: namespace, DefaultPolicySets: []string{testDefaultPolicy},
		Presets: json.RawMessage(testPresets),
	}, kube, dispatcher.client(t))
	if err != nil {
		t.Fatal(err)
	}
	return fixture{
		namespace: namespace, caller: store.ServiceAccount{Namespace: namespace, Name: "test-caller"},
		dispatcher: dispatcher, executor: executor,
	}
}

// attempted is how one attempt ended.
type attempted struct {
	t       *testing.T
	outcome store.ExecutionOutcome
	err     error
}

// attempt runs one attempt at action as caller. Execute is attempt under worker.Hold, whose lease only the worker
// makes.
func (f fixture) attempt(t *testing.T, caller store.ServiceAccount, action, arguments string) attempted {
	t.Helper()
	outcome, err := f.executor.attempt(t.Context(), store.ExecutionRequest{
		RequestID: uuid.New(), Action: store.ActionIdentity{Group: testGroup, Name: action},
		Arguments: json.RawMessage(arguments), Caller: caller,
	})
	return attempted{t, outcome, err}
}

// launch launches a Sandbox of name as the fixture's caller, granting it grants.
func (f fixture) launch(t *testing.T, name string, grants ...string) SandboxRef {
	t.Helper()
	arguments, err := json.Marshal(launchArgs{Name: name, Template: "test-template", LauncherGrants: grants})
	if err != nil {
		t.Fatal(err)
	}
	return resultOf[Launched](f.attempt(t, f.caller, actionLaunchSandbox, string(arguments))).Sandbox
}

// resultOf is the result of an attempt that succeeded.
func resultOf[T any](a attempted) T {
	a.t.Helper()
	if a.err != nil || a.outcome.State != store.ExecutionSucceeded {
		a.t.Fatalf("attempt = %s %s, %v; want it to succeed", a.outcome.State, a.outcome.Error, a.err)
	}
	var result T
	if err := json.Unmarshal(a.outcome.Result, &result); err != nil {
		a.t.Fatal(err)
	}
	return result
}

// failure is the kind of an attempt that failed with a reason the caller can act on.
func (a attempted) failure() string {
	a.t.Helper()
	if a.err != nil || a.outcome.State != store.ExecutionFailed {
		a.t.Fatalf("attempt = %s %s, %v; want it to fail", a.outcome.State, a.outcome.Result, a.err)
	}
	var failure struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(a.outcome.Error, &failure); err != nil {
		a.t.Fatal(err)
	}
	return failure.Kind
}

// recordingDispatcher is a Session Dispatcher that records each request and answers with a Dispatch of it, or with
// refusal while that is set.
type recordingDispatcher struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	mu       sync.Mutex
	requests []proto.Message
	refusal  error
}

// client serves the Dispatcher in memory until the test ends and is a client of it.
func (d *recordingDispatcher) client(t *testing.T) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	grpcServer := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(grpcServer, d)
	go grpcServer.Serve(listener)
	t.Cleanup(grpcServer.Stop)
	connection, err := grpc.NewClient("passthrough:///test-dispatcher",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

func (d *recordingDispatcher) record(request proto.Message) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.requests = append(d.requests, request)
	return d.refusal
}

func (d *recordingDispatcher) refuse(err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.refusal = err
}

// received are the requests the Dispatcher was sent, in order.
func (d *recordingDispatcher) received() []proto.Message {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]proto.Message{}, d.requests...)
}

func (d *recordingDispatcher) OpenSession(_ context.Context, request *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	return &dispatcherpb.OpenSessionResponse{Opening: &dispatcherpb.SessionOpening{
		SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(),
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (d *recordingDispatcher) SubmitInput(_ context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(),
		SandboxUid: request.GetSandboxUid(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (d *recordingDispatcher) InterruptTurn(_ context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP}, nil
}
