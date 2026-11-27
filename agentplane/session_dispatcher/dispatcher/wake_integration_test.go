package dispatcher

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// An unattributed dispatch (historically, one attributed only to a ServiceAccount) is refused a
// wake without consulting the lifecycle manager.
func TestUnattributedDispatchDoesNotWake(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000092"
	const namespace = "test-historical-wake"
	row, err := enqueueWithSession(t, queueStore, ctx, "historical-wake-session", "historical-wake-sandbox", uid,
		input("historical-wake-command", "hello"))
	if err != nil {
		t.Fatal("enqueue attributed row:", err)
	}
	_, err = queueStore.DB.ExecContext(ctx, `UPDATE dispatch SET
		service_attribution=NULL,
		actor_identity_name=NULL,actor_identity_uid=NULL,actor_identity_pod_name=NULL,actor_identity_pod_uid=NULL
		WHERE command_id=$1`, row.CommandID)
	if err != nil {
		t.Fatal("seed unattributed dispatch:", err)
	}
	row, err = queueStore.GetDispatch(ctx, row.CommandID)
	if err != nil || row.Attribution != nil {
		t.Fatalf("read unattributed dispatch: %+v %v", row, err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, row.SessionID)
	wake := &identityWake{}
	worker := &DeliveryWorker{Store: queueStore, Wake: wake}
	worker.init()
	defer worker.queue.ShutDown()
	_, err = worker.prepareDispatch(ctx, row, SandboxSnapshot{
		Name: "historical-wake-sandbox", Namespace: namespace, UID: uid,
		Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
	}, deliveryOwner)
	if err != nil {
		t.Fatal("prepare unattributed dispatch:", err)
	}
	stored, err := queueStore.GetDispatch(ctx, row.CommandID)
	if err != nil {
		t.Fatal("read refused dispatch:", err)
	}
	if stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED ||
		stored.WakeAssessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED ||
		len(wake.callers) != 0 {
		t.Fatalf("unattributed dispatch gained wake authority: assessment=%+v wake calls=%v",
			stored.WakeAssessment, wake.callers)
	}
}

// identityWake records whom Dispatcher asks the lifecycle manager to wake for, and grants it.
type identityWake struct {
	callers []lifecycle.WakeCaller
}

func (w *identityWake) RequestWake(_ context.Context, _ *lifecycle.Target, caller lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	w.callers = append(w.callers, caller)
	return &environmentpb.WakeAssessment{
		State: environmentpb.WakeState_WAKE_STATE_REQUESTED, ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(1),
	}, nil
}

// Queued input from an Identity's own workload wakes the suspended recipient Sandbox as that
// Identity: the lifecycle manager is asked for the Identity.
func TestAnIdentitySenderWakesASuspendedRecipient(t *testing.T) {
	ctx := context.Background()
	identity := &identitypb.Identity{Name: "identity-sender", Uid: uuid.NewString()}
	queueStore := testStore(t, 3)
	sessionID, sandboxName, sandboxUID := "identity-wake-session-"+uuid.NewString()[:8], "identity-wake-sandbox", uuid.NewString()
	// Only the Identity's own workload opens the Session here (a seed: in production a privileged
	// caller does); the sender under test then queues input to it.
	if _, err := createSessionForCaller(queueStore, ctx, sessionID, sandboxName, sandboxUID, &sessionpb.SessionSpec{}, nil,
		identity, store.IdentityCaller{Identity: identity, Proof: store.IdentitySVID{}}); err != nil {
		t.Fatal("open the Identity-run Session:", err)
	}
	sender := store.IdentityCaller{Identity: identity, Proof: store.IdentitySVID{Pod: &store.PodIdentity{Name: "sender-pod", UID: "sender-pod-uid"}}}
	row, err := queueStore.Enqueue(ctx, sessionID, sandboxName, sandboxUID, input("identity-wake-command-"+uuid.NewString()[:8], "hello"), sender)
	if err != nil {
		t.Fatal("enqueue the Identity sender's input:", err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, sessionID)
	wake := &identityWake{}
	worker := &DeliveryWorker{Store: queueStore, Wake: wake}
	worker.init()
	defer worker.queue.ShutDown()
	ready, err := worker.prepareDispatch(ctx, row, SandboxSnapshot{
		Name: sandboxName, Namespace: "agentplane", UID: sandboxUID,
		Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
	}, deliveryOwner)
	if err != nil || ready {
		t.Fatalf("prepare the Identity sender's input: ready=%v error=%v", ready, err)
	}
	if len(wake.callers) != 1 || !sameWakeCaller(wake.callers[0], lifecycle.IdentityWakeCaller{Identity: identity}) {
		t.Fatalf("lifecycle asked to wake for %+v, want the sender's Identity %v", wake.callers, identity)
	}
	stored, err := queueStore.GetDispatch(ctx, row.CommandID)
	if err != nil || stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || stored.WakeRequestedAt == nil {
		t.Fatalf("queued input after the wake: %+v, %v; want a requested wake", stored, err)
	}
}
