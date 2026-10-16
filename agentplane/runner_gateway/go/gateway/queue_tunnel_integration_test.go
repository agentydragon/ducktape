package gateway

import (
	"context"
	"database/sql"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/dispatcher"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
)

const queueSandboxUID = "00000000-0000-4000-8000-000000000121"

type queueCatalog struct{ sandbox dispatcher.SandboxSnapshot }

func (c queueCatalog) GetSandbox(_ context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c queueCatalog) ListSandboxes(context.Context) ([]dispatcher.SandboxSnapshot, error) {
	return []dispatcher.SandboxSnapshot{c.sandbox}, nil
}
func (queueCatalog) Changes() <-chan struct{}        { return nil }
func (queueCatalog) WaitSynced(context.Context) bool { return true }

type queueWake struct{}

func (queueWake) WakeForSend(context.Context, string, string, func(int64) error) (bool, error) {
	return false, errors.New("unexpected wake for ready Sandbox")
}

type queueLease struct{ store *dispatcher.Store }

func (l queueLease) Acquire(ctx context.Context, _, uid string) (*dispatcher.Owner, error) {
	generation, err := l.store.OwnershipGeneration(ctx, uid)
	if err != nil {
		return nil, err
	}
	return l.store.ClaimSandboxOwner(ctx, uid, "queue-test-lease", 1, "queue-test-replica", generation)
}

func (l queueLease) StillOwns(ctx context.Context, owner dispatcher.Owner) (bool, error) {
	return l.store.IsCurrentOwner(ctx, owner)
}

// This test runs via the PostgreSQL-backed Python wrapper below. It starts with
// a committed queue row and ends with the runner's admission returned over the
// real Gateway tunnel, without bypassing the Dispatcher worker.
func TestQueuedDeliveryThroughGatewayTunnel(t *testing.T) {
	dsn := os.Getenv("AGENTPLANE_TEST_DATABASE_URL")
	if dsn == "" {
		t.Fatal("AGENTPLANE_TEST_DATABASE_URL is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	store := dispatcher.NewStore(db, 4)
	command := &commandpb.Command{CommandId: "queue-tunnel-command", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "persisted input"},
	}}
	queued, err := store.Enqueue(ctx, "session-queue", testSandbox, queueSandboxUID, command, nil)
	if err != nil || queued.State != dispatcher.QueueState {
		t.Fatalf("queue receipt: %+v %v", queued, err)
	}

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
		ObjectMeta: metav1.ObjectMeta{Name: testSandbox, Namespace: testNamespace, UID: queueSandboxUID},
		Status: sandboxapi.SandboxStatus{
			OperatingMode: sandboxapi.OperatingModeRunning,
			Incarnation:   &sandboxapi.Incarnation{Name: testPod, UID: testPodUID},
			Conditions:    []metav1.Condition{{Type: sandboxapi.ConditionRunning, Status: metav1.ConditionTrue, Reason: "Ready"}},
		},
	}
	controller := true
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: testPod, Namespace: testNamespace, UID: types.UID(testPodUID),
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: testSandbox, UID: types.UID(queueSandboxUID), Controller: &controller,
			}},
		},
		Spec: corev1.PodSpec{ServiceAccountName: testSandbox},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{
			Type: corev1.PodReady, Status: corev1.ConditionTrue,
		}}},
	}
	objects := newSandboxClient(t, sandbox)
	kube := kubernetesfake.NewSimpleClientset(pod)
	gateway, err := New(Config{
		Namespace: testNamespace, CallerAccounts: []auth.Account{{Namespace: "apps", Name: "app"}},
		HolderName: "runner-gateway-0", HolderIP: "127.0.0.1", ProxyPort: sandboxapi.ControlServicePort,
		ControlTiming: ControlTiming{
			HeartbeatInterval: time.Second, LivenessTimeout: 10 * time.Second,
			LeaseTouchInterval: 5 * time.Second, MaximumAge: time.Minute,
		},
	}, callerAuth, runnerAuth, objects, kube.CoreV1(), kube.CoordinationV1(), kube.DiscoveryV1())
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	gateway.RegisterProxy(server)
	gateway.RegisterRunnerControl(server)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	defer listener.Close()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	streamCtx := metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer runner-token"))
	stream, err := runnercontrolpb.NewRunnerControlClient(connection).Connect(streamCtx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Hello{
		Hello: &runnercontrolpb.Hello{RunnerBootId: "queue-boot"},
	}}); err != nil {
		t.Fatal(err)
	}
	if frame, err := stream.Recv(); err != nil || frame.GetWelcome() == nil {
		t.Fatalf("runner welcome: %v %v", frame, err)
	}

	tokenFile := filepath.Join(t.TempDir(), "caller-token")
	if err := os.WriteFile(tokenFile, []byte("app-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	hub := dispatcher.NewWatchHub(dsn)
	hubDone := make(chan error, 1)
	go func() { hubDone <- hub.Run(ctx) }()
	for !hub.Connected() {
		if err := ctx.Err(); err != nil {
			t.Fatalf("PostgreSQL listener unavailable: %v", err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	worker := &dispatcher.DeliveryWorker{
		Store: store, Catalog: queueCatalog{dispatcher.SandboxSnapshot{
			Name: testSandbox, Namespace: testNamespace, UID: queueSandboxUID, Incarnation: testPodUID,
		}}, Wake: queueWake{}, Leases: queueLease{store: store},
		Runner: &dispatcher.ProxyRunner{
			Namespace: testNamespace, TokenFile: tokenFile, Timeout: 5 * time.Second,
			TargetTemplate: listener.Addr().String(),
		},
		Hub: hub, Ready: make(chan struct{}),
	}
	workerDone := make(chan error, 1)
	go func() { workerDone <- worker.Run(ctx) }()
	select {
	case <-worker.Ready:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}

	resume := receiveTunnelCall(t, stream)
	if resume.GetMethod() != "/agentplane.proto.runner_control.Runner/ResumeSession" {
		t.Fatalf("queued command did not resume runner: %v", resume)
	}
	resumeResponse, err := proto.Marshal(&runnercontrolpb.ResumeSessionResponse{Attached: &runnerpb.Attached{SessionId: "session-queue"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: resume.GetCallId(), Response: resumeResponse},
	}}); err != nil {
		t.Fatal(err)
	}
	input := receiveTunnelCall(t, stream)
	var request runnercontrolpb.SubmitInputRequest
	if err := proto.Unmarshal(input.GetRequest(), &request); err != nil {
		t.Fatal(err)
	}
	if input.GetMethod() != "/agentplane.proto.runner_control.Runner/SubmitInput" ||
		request.GetCommandId() != command.GetCommandId() || request.GetSessionId() != "session-queue" ||
		request.GetText() != "persisted input" {
		t.Fatalf("queued request changed before runner admission: call=%v request=%v", input, &request)
	}
	admission, err := proto.Marshal(&runnercontrolpb.SubmitInputResponse{Admission: &eventlogpb.EventEntry{Cursor: 81}})
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&runnercontrolpb.RunnerFrame{Frame: &runnercontrolpb.RunnerFrame_Reply{
		Reply: &tunnelpb.Reply{CallId: input.GetCallId(), Response: admission},
	}}); err != nil {
		t.Fatal(err)
	}
	for {
		row, err := store.GetDispatch(ctx, command.GetCommandId())
		if err != nil {
			t.Fatal(err)
		}
		if row.State == dispatcher.AdmittedState {
			if row.Admission.GetCursor() != 81 {
				t.Fatalf("wrong stored runner admission: %+v", row)
			}
			break
		}
		if err := ctx.Err(); err != nil {
			t.Fatalf("queued delivery never admitted: %+v %v", row, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-workerDone:
	case <-time.After(time.Second):
		t.Fatal("worker did not stop")
	}
	select {
	case <-hubDone:
	case <-time.After(time.Second):
		t.Fatal("PostgreSQL listener did not stop")
	}
}
