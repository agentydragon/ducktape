package dispatcher

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestActionAttributedSendUsesActionsBrokerForIdleSuspendedSandbox(t *testing.T) {
	for _, tc := range []struct {
		name     string
		broker   bool
		wantWake bool
	}{
		{name: "broker configured", broker: true, wantWake: true},
		{name: "broker absent", wantWake: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			api := kubeapiserver.StartForTest(t)

			scheme := runtime.NewScheme()
			for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme} {
				if err := add(scheme); err != nil {
					t.Fatal(err)
				}
			}
			objects, err := client.New(api.Config, client.Options{Scheme: scheme})
			if err != nil {
				t.Fatal(err)
			}

			ctx := t.Context()
			namespace := "fake-action-wake-" + uuid.NewString()[:8]
			if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
				t.Fatal(err)
			}
			identity := &identitypb.Identity{Name: "fake-action-owner", Uid: "00000000-0000-4000-8000-000000000042"}
			const generation = int64(19)
			idle := sandboxapi.SuspendedByIdle
			sandbox := &sandboxapi.Sandbox{
				ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "fake-action-sandbox"},
				Spec: sandboxapi.SandboxSpec{
					Template: "fake-template", Running: true, WakeOnSend: true, WakeGeneration: generation,
					Identity: &sandboxapi.IdentityRef{Name: identity.GetName()},
				},
			}
			if err := objects.Create(ctx, sandbox); err != nil {
				t.Fatal(err)
			}
			sandbox.Status = sandboxapi.SandboxStatus{
				OperatingMode:             sandboxapi.OperatingModeSuspended,
				StopStage:                 sandboxapi.StopStageStopped,
				SuspendedBy:               &idle,
				SuspendedAtWakeGeneration: proto.Int64(generation),
			}
			if err := objects.Status().Update(ctx, sandbox); err != nil {
				t.Fatal(err)
			}

			queueStore := testStore(t, 3)
			identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
			if err != nil {
				t.Fatal(err)
			}
			managerOptions := wake.Options{Namespace: namespace, Objects: objects, Identities: identities, WakeBudget: queueStore}
			if tc.broker {
				managerOptions.WakeServiceCallers = []string{peertls.ActionsURI}
			}
			manager, err := wake.NewManager(managerOptions)
			if err != nil {
				t.Fatal(err)
			}

			sessionID, commandID := uuid.NewString(), uuid.NewString()
			spec := &sessionpb.SessionSpec{}
			testPlacement(spec, sandbox.Name, string(sandbox.UID))
			caller := store.IdentityCaller{
				Identity: identity,
				Proof: store.IdentityServiceDelegation{
					ActorSPIFFEID: peertls.ActionsURI,
					Action:        store.ActionCorrelation{RequestID: "fake-action-request", ExecutionID: "fake-action-execution"},
				},
			}
			if _, err := queueStore.CreateSessionWithPlacementForCaller(ctx, sessionID, sandbox.Name, string(sandbox.UID), spec, nil, nil,
				store.SessionExecution{Identity: identity}, caller); err != nil {
				t.Fatal("create Action-owned Session:", err)
			}
			row, err := queueStore.EnqueueForCaller(ctx, sessionID, sandbox.Name, string(sandbox.UID),
				input(commandID, "fake action send"), nil, caller)
			if err != nil {
				t.Fatal("enqueue Action-attributed send:", err)
			}
			owner := claimSessionOwner(t, queueStore, sessionID)
			worker := &DeliveryWorker{Store: queueStore, Wake: manager}
			worker.init()
			t.Cleanup(worker.queue.ShutDown)
			ready, err := worker.prepareDispatch(ctx, row, SandboxSnapshot{
				Namespace: namespace, Name: sandbox.Name, UID: string(sandbox.UID),
				Incarnation: "fake-old-incarnation", Blocker: "sandbox_suspended", WakeOnSend: true,
				WakeGeneration: generation,
			}, owner)
			if err != nil || ready {
				t.Fatalf("prepare Action-attributed send: ready=%v error=%v", ready, err)
			}

			stored, err := queueStore.GetDispatch(ctx, commandID)
			if err != nil {
				t.Fatal("read wake assessment:", err)
			}
			var current sandboxapi.Sandbox
			if err := objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &current); err != nil {
				t.Fatal(err)
			}
			if tc.wantWake {
				if stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED ||
					current.Spec.WakeGeneration != generation+1 || !current.Spec.Running {
					t.Fatalf("Action broker did not wake Sandbox: assessment=%+v spec=%+v", stored.WakeAssessment, current.Spec)
				}
				if stored.WakeAssessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER {
					t.Fatalf("Action broker assessment reason = %v, want allowed broker", stored.WakeAssessment.GetReason())
				}
			} else if stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED ||
				stored.WakeAssessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED ||
				current.Spec.WakeGeneration != generation {
				t.Fatalf("unconfigured Action broker was not denied: assessment=%+v spec=%+v", stored.WakeAssessment, current.Spec)
			}
		})
	}
}
