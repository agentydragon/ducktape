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
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type launchTokenReview struct{ namespace string }

func (r launchTokenReview) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: request.Spec.Token == "action-fixture-token", Audiences: request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + r.namespace + ":actions", UID: "actions-uid",
			Extra: map[string]authenticationv1.ExtraValue{"authentication.kubernetes.io/pod-name": {"actions-pod"}, "authentication.kubernetes.io/pod-uid": {"actions-pod-uid"}},
		},
	}}, nil
}

type unusedWakeBudget struct{}

func (unusedWakeBudget) ReserveWakeAttempt(context.Context, lifecycle.WakeBudgetKey, string, int64) (time.Duration, error) {
	return 0, nil
}

const (
	testGroup         = "test-orchestration"
	testDefaultPolicy = "test-default-policy"
	testExtraPolicy   = "test-extra-policy"
	testPresets       = `{"test-preset": {
		"title": "Test preset",
		"launch": {"template": "test-template", "policy_sets": ["test-extra-policy"]},
		"create_session": {"spec": {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/test/{session_id}"}}
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
		for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme, corev1.AddToScheme} {
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
// Dispatcher and History Service it calls.
type fixture struct {
	namespace  string
	caller     store.ServiceAccount
	dispatcher *recordingDispatcher
	history    *fakeHistory
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
	dispatcher := &recordingDispatcher{openings: map[string]*dispatcherpb.Session{}}
	history := &fakeHistory{summaries: map[string]*historypb.SessionSummary{}}
	objects, err := client.NewWithWatch(server.Config, client.Options{Scheme: kube.Scheme()})
	if err != nil {
		t.Fatal(err)
	}
	manager, err := lifecycle.NewManager(lifecycle.Options{Namespace: namespace, Objects: objects, WakeBudget: unusedWakeBudget{}})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environments.New(namespace, objects, runtimeconfig.Catalog{}, manager, nil, environments.ManagementOptions{
		Launchers: sets.New(auth.Account{Namespace: namespace, Name: "actions"}),
	})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(launchTokenReview{namespace}, "environment-test", []string{namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods := environments.Methods(resolver)
	environment := environmentpb.NewEnvironmentServiceClient(serve(t, func(s *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(s, service) }, methods.ServerOptions(0)...))
	executor, err := NewExecutor(testGroup, catalog.OrchestrationBinding{
		Description: "test orchestration", Namespace: namespace,
		Presets: json.RawMessage(testPresets),
	}, kube, environment,
		dispatcherpb.NewSessionDispatcherClient(serve(t, func(s *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(s, dispatcher) })),
		historypb.NewHistoryServiceClient(serve(t, func(s *grpc.Server) { historypb.RegisterHistoryServiceServer(s, history) })), 30*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return fixture{
		namespace: namespace, caller: store.ServiceAccount{Namespace: namespace, Name: "test-caller"},
		dispatcher: dispatcher, history: history, executor: executor,
	}
}

// serve serves what register registers in memory until the test ends, and is a connection to it.
func serve(t *testing.T, register func(*grpc.Server), serverOptions ...grpc.ServerOption) *grpc.ClientConn {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	grpcServer := grpc.NewServer(serverOptions...)
	register(grpcServer)
	go grpcServer.Serve(listener)
	t.Cleanup(grpcServer.Stop)
	connection, err := grpc.NewClient("passthrough:///test-peer",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithUnaryInterceptor(func(ctx context.Context, method string, req, reply any, conn *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
			return invoke(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer action-fixture-token"), method, req, reply, conn, opts...)
		}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return connection
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
		Arguments: json.RawMessage(arguments), Caller: store.ServiceAccountCaller{Account: caller},
	})
	return attempted{t, outcome, err}
}

// launch launches a Sandbox of name as the fixture's caller, granting it grants.
func (f fixture) launch(t *testing.T, name string, grants ...string) contract.SandboxRef {
	t.Helper()
	arguments, err := json.Marshal(contract.LaunchArgs{Name: name, Template: "test-template", LauncherGrants: grants})
	if err != nil {
		t.Fatal(err)
	}
	return resultOf[Launched](f.attempt(t, f.caller, contract.ActionLaunchSandbox, string(arguments))).Sandbox
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
func (a attempted) failure() action_servicepb.ExecutionErrorKind {
	a.t.Helper()
	if a.err != nil || a.outcome.State != store.ExecutionFailed {
		a.t.Fatalf("attempt = %s %s, %v; want it to fail", a.outcome.State, a.outcome.Result, a.err)
	}
	return a.outcome.Error.GetKind()
}

// recordingDispatcher is a Session Dispatcher that records each command and answers with a Dispatch of it, or with
// refusal while that is set. Its reads answer from the openings and Dispatches a test has it hold.
type recordingDispatcher struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	mu         sync.Mutex
	requests   []proto.Message
	refusal    error
	openings   map[string]*dispatcherpb.Session
	dispatches []*dispatcherpb.Dispatch
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

func (d *recordingDispatcher) CreateSession(_ context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	opening := &dispatcherpb.Session{
		SessionId: request.GetSessionId(), Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING},
	}
	opening.Placement = request.GetSpec().GetPlacement()
	return &dispatcherpb.CreateSessionResponse{Session: opening}, nil
}

func (d *recordingDispatcher) SubmitInput(_ context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), SessionId: request.GetSessionId(),
		SandboxUid: request.GetExpectedSandboxUid(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

// hold has the Dispatcher hold opening, and dispatches after those it holds.
func (d *recordingDispatcher) hold(opening *dispatcherpb.Session, dispatches ...*dispatcherpb.Dispatch) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if opening != nil {
		d.openings[opening.GetSessionId()] = opening
	}
	d.dispatches = append(d.dispatches, dispatches...)
}

func (d *recordingDispatcher) GetSession(_ context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	opening, held := d.openings[request.GetSessionId()]
	if !held {
		return nil, status.Error(codes.NotFound, "dispatch not found")
	}
	return &dispatcherpb.GetSessionResponse{Session: opening}, nil
}

// ListDispatches answers a Session's Dispatches, oldest first.
func (d *recordingDispatcher) ListDispatches(_ context.Context, request *dispatcherpb.ListDispatchesRequest) (*dispatcherpb.ListDispatchesResponse, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	response := &dispatcherpb.ListDispatchesResponse{}
	for _, dispatch := range d.dispatches {
		if dispatch.GetSessionId() == request.GetFilter().GetSessionId() {
			response.Dispatches = append(response.Dispatches, dispatch)
		}
	}
	return response, nil
}

func (d *recordingDispatcher) InterruptTurn(_ context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	if err := d.record(request); err != nil {
		return nil, err
	}
	return &dispatcherpb.InterruptTurnResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId(), SessionId: request.GetSessionId(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER}}, nil
}

// fakeHistory is a History Service holding the summaries a test gives it, or refusing every read while refusal is set.
type fakeHistory struct {
	historypb.UnimplementedHistoryServiceServer
	mu        sync.Mutex
	summaries map[string]*historypb.SessionSummary
	refusal   error
	follow    func(*historypb.FollowSessionRequest, grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error
}

func (h *fakeHistory) hold(summaries ...*historypb.SessionSummary) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, summary := range summaries {
		h.summaries[summary.GetSessionId()] = summary
	}
}

func (h *fakeHistory) refuse(err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.refusal = err
}

func (h *fakeHistory) GetSessionSummary(_ context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.refusal != nil {
		return nil, h.refusal
	}
	summary, held := h.summaries[request.GetSessionId()]
	if !held {
		return nil, status.Error(codes.NotFound, "session history not found")
	}
	return summary, nil
}

// BatchGetSessionSummaries refuses more than 200 Sessions, as the History Service does.
func (h *fakeHistory) BatchGetSessionSummaries(_ context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.refusal != nil {
		return nil, h.refusal
	}
	if len(request.GetSessionIds()) > 200 {
		return nil, status.Error(codes.InvalidArgument, "too many Sessions in one summary read")
	}
	response := &historypb.BatchGetSessionSummariesResponse{}
	for _, id := range request.GetSessionIds() {
		if summary, held := h.summaries[id]; held {
			response.Summaries = append(response.Summaries, summary)
		}
	}
	return response, nil
}

// FindSessions finds by Sandbox UID only, the one match the executor uses.
func (h *fakeHistory) FindSessions(_ context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.refusal != nil {
		return nil, h.refusal
	}
	response := &historypb.FindSessionsResponse{}
	for id, summary := range h.summaries {
		if summary.GetSandbox().GetUid() == request.GetSandboxUid() {
			response.Sessions = append(response.Sessions, &historypb.SessionSandbox{SessionId: id, Sandbox: summary.GetSandbox()})
		}
	}
	return response, nil
}

func (h *fakeHistory) FollowSession(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
	h.mu.Lock()
	follow, refused := h.follow, h.refusal
	h.mu.Unlock()
	if refused != nil {
		return refused
	}
	if follow == nil {
		return status.Error(codes.Unimplemented, "no test follow")
	}
	return follow(request, stream)
}
