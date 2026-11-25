package dispatcher

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type callbackWake struct {
	queueStore *store.Store
	calls      int
	callers    []*lifecycle.ServiceAccountIdentity
}

func (w *callbackWake) RequestWake(
	ctx context.Context,
	target *lifecycle.Target,
	caller lifecycle.WakeCaller,
) (*environmentpb.WakeAssessment, error) {
	w.calls++
	w.callers = append(w.callers, wakeAccount(caller))
	wait, err := w.queueStore.ReserveWakeAttempt(ctx, wakeKey(wakeAccount(caller)), target.GetUid(), 41)
	if err != nil {
		return nil, err
	}
	assessment := &environmentpb.WakeAssessment{
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(41),
	}
	if wait > 0 {
		assessment.State = environmentpb.WakeState_WAKE_STATE_RATE_LIMITED
		assessment.Reason = environmentpb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED
		assessment.RetryAt = timestamppb.New(time.Now().Add(wait))
		return assessment, nil
	}
	assessment.State = environmentpb.WakeState_WAKE_STATE_REQUESTED
	return assessment, nil
}

func wakeAttribution(actor, effective *identitypb.ServiceAccountIdentity) *store.AdmissionAttribution {
	return &store.AdmissionAttribution{
		Actor: actor, EffectiveCaller: effective,
		ActorPod: &store.PodIdentity{Name: "actions-pod", UID: "actions-pod-uid"},
		Action:   &store.ActionCorrelation{RequestID: uuid.NewString(), ExecutionID: uuid.NewString()},
	}
}

func openWakeableSession(
	t *testing.T,
	queueStore *store.Store,
	sessionID, sandboxName, sandboxUID string,
	actor, effective *identitypb.ServiceAccountIdentity,
) *store.AdmissionAttribution {
	t.Helper()
	attribution := wakeAttribution(actor, effective)
	effectiveAccount := accountOfIdentity(effective)
	if _, err := createSessionForCaller(queueStore, context.Background(), sessionID, sandboxName, sandboxUID,
		&sessionpb.SessionSpec{}, nil, &effectiveAccount, testExecutionIdentity(), store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution}); err != nil {
		t.Fatal("open wakeable Session:", err)
	}
	return attribution
}

func TestDeliveryUsesRetainedEffectiveCallerUIDForWakeBudget(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	actor := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "actions"}, "actions-uid")
	effective := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "delegate"}, "delegate-uid")
	wake := &callbackWake{queueStore: queueStore}
	worker := &DeliveryWorker{Store: queueStore, Wake: wake}
	worker.init()
	defer worker.queue.ShutDown()

	for i, kind := range []string{"dispatch", "opening"} {
		sessionID := fmt.Sprintf("uid-wake-session-%d", i)
		sandboxName, sandboxUID := fmt.Sprintf("uid-wake-sandbox-%d", i), uuid.NewString()
		attribution := openWakeableSession(t, queueStore, sessionID, sandboxName, sandboxUID, actor, effective)
		sandbox := SandboxSnapshot{
			Name: sandboxName, Namespace: effective.GetAccount().GetNamespace(), UID: sandboxUID,
			Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
		}
		deliveryOwner := claimSessionOwner(t, queueStore, sessionID)
		var ready bool
		var prepareErr error
		if kind == "dispatch" {
			caller := store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution}
			effectiveAccount := accountOfIdentity(effective)
			row, err := queueStore.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID,
				input("uid-wake-command", "hello"), &effectiveAccount, caller)
			if err != nil {
				t.Fatal("enqueue attributed wake command:", err)
			}
			ready, prepareErr = worker.prepareDispatch(ctx, row, sandbox, deliveryOwner)
		} else {
			row, err := queueStore.GetSession(ctx, sessionID)
			if err != nil {
				t.Fatal("read attributed wake opening:", err)
			}
			ready, prepareErr = worker.prepareSession(ctx, row, sandbox, deliveryOwner)
		}
		if prepareErr != nil || ready {
			t.Fatalf("prepare %s: ready=%v error=%v", kind, ready, prepareErr)
		}
		var effectiveRows, actorRows int
		if err := queueStore.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			effective.GetAccount().GetNamespace(), effective.GetAccount().GetName(), effective.GetUid(), sandboxUID).Scan(&effectiveRows); err != nil {
			t.Fatal("count effective caller wake attempts:", err)
		}
		if err := queueStore.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			actor.GetAccount().GetNamespace(), actor.GetAccount().GetName(), actor.GetUid(), sandboxUID).Scan(&actorRows); err != nil {
			t.Fatal("count actor wake attempts:", err)
		}
		if effectiveRows != 1 || actorRows != 0 {
			t.Fatalf("%s wake budget used effective=%d actor=%d rows", kind, effectiveRows, actorRows)
		}
		if got := wake.callers[i]; !proto.Equal(got, effective) {
			t.Fatalf("%s lifecycle caller=%+v, want retained effective identity", kind, got)
		}
	}
	if wake.calls != 2 {
		t.Fatalf("wake service calls=%d, want both queued delivery paths", wake.calls)
	}
}

func TestDispatchWithoutCallerIdentityDoesNotWake(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000092"
	wakeCaller := auth.Account{Namespace: "agentplane", Name: "historical-wake-caller"}
	row, err := enqueueWithSession(t, queueStore, ctx, "historical-wake-session", "historical-wake-sandbox", uid,
		input("historical-wake-command", "hello"), nil)
	if err != nil {
		t.Fatal("enqueue attributed row:", err)
	}
	_, err = queueStore.DB.ExecContext(ctx, `UPDATE dispatch SET
		actor_account_namespace=NULL,actor_account_name=NULL,actor_account_uid=NULL,actor_pod_name=NULL,actor_pod_uid=NULL,
		effective_caller_account_namespace=NULL,effective_caller_account_name=NULL,effective_caller_account_uid=NULL,
		action_request_id=NULL,action_execution_id=NULL,
		actor_identity_name=NULL,actor_identity_uid=NULL,actor_identity_pod_name=NULL,actor_identity_pod_uid=NULL,
		delegated_identity_name=NULL,delegated_identity_uid=NULL,
		delegator_account_namespace=NULL,delegator_account_name=NULL,delegator_account_uid=NULL,
		delegator_pod_name=NULL,delegator_pod_uid=NULL,
		delegated_action_request_id=NULL,delegated_action_execution_id=NULL,
		wake_caller_namespace=$2,wake_caller_name=$3 WHERE command_id=$1`, row.CommandID, wakeCaller.Namespace, wakeCaller.Name)
	if err != nil {
		t.Fatal("seed historical wake caller without caller Identity:", err)
	}
	row, err = queueStore.GetDispatch(ctx, row.CommandID)
	if err != nil || row.Attribution != nil {
		t.Fatalf("read historical dispatch without caller Identity: %+v %v", row, err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, row.SessionID)
	wake := &callbackWake{queueStore: queueStore}
	worker := &DeliveryWorker{Store: queueStore, Wake: wake}
	worker.init()
	defer worker.queue.ShutDown()
	_, err = worker.prepareDispatch(ctx, row, SandboxSnapshot{
		Name: "historical-wake-sandbox", Namespace: wakeCaller.Namespace, UID: uid,
		Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
	}, deliveryOwner)
	if err != nil {
		t.Fatal("prepare dispatch without caller Identity:", err)
	}
	stored, err := queueStore.GetDispatch(ctx, row.CommandID)
	if err != nil {
		t.Fatal("read refused dispatch:", err)
	}
	var attempts int
	if err := queueStore.DB.QueryRowContext(ctx, "SELECT count(*) FROM wake_attempt WHERE sandbox_uid=$1", uid).Scan(&attempts); err != nil {
		t.Fatal("count wake attempts:", err)
	}
	if stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED ||
		stored.WakeAssessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED ||
		wake.calls != 0 || attempts != 0 {
		t.Fatalf("wake caller without caller Identity gained wake authority: assessment=%+v wake calls=%d attempts=%d",
			stored.WakeAssessment, wake.calls, attempts)
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

// Queued input from an Identity sender, its own workload or a component acting for it, wakes the
// suspended recipient Sandbox as that Identity: the lifecycle manager is asked for the Identity,
// as it is asked for a ServiceAccount sender's account.
func TestAnIdentitySenderWakesASuspendedRecipient(t *testing.T) {
	ctx := context.Background()
	identity := &identitypb.Identity{Name: "identity-sender", Uid: uuid.NewString()}
	actor := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "actions"}, "actions-uid")
	for name, proof := range map[string]store.IdentityProof{
		"own workload": store.IdentitySVID{Pod: &store.PodIdentity{Name: "sender-pod", UID: "sender-pod-uid"}},
		"acted for": store.IdentityDelegation{
			Actor: actor, ActorPod: store.PodIdentity{Name: "actions-pod", UID: "actions-pod-uid"},
			Action: store.ActionCorrelation{RequestID: uuid.NewString(), ExecutionID: uuid.NewString()},
		},
	} {
		t.Run(name, func(t *testing.T) {
			queueStore := testStore(t, 3)
			sessionID, sandboxName, sandboxUID := "identity-wake-session-"+uuid.NewString()[:8], "identity-wake-sandbox", uuid.NewString()
			spec := &sessionpb.SessionSpec{}
			// Only the Identity's own workload opens the Session here (a seed: in production a privileged
			// caller does); the sender under test then queues input to it.
			testPlacement(spec, sandboxName, sandboxUID)
			if _, err := queueStore.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID,
				spec, nil, nil, store.SessionExecution{Identity: identity},
				store.IdentityCaller{Identity: identity, Proof: store.IdentitySVID{}}); err != nil {
				t.Fatal("open the Identity-run Session:", err)
			}
			sender := store.IdentityCaller{Identity: identity, Proof: proof}
			row, err := queueStore.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, input("identity-wake-command-"+uuid.NewString()[:8], "hello"), nil, sender)
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
			if len(wake.callers) != 1 || !proto.Equal(wake.callers[0].(lifecycle.IdentityWakeCaller).Identity, identity) {
				t.Fatalf("lifecycle asked to wake for %+v, want the sender's Identity %v", wake.callers, identity)
			}
			stored, err := queueStore.GetDispatch(ctx, row.CommandID)
			if err != nil || stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || stored.WakeRequestedAt == nil {
				t.Fatalf("queued input after the wake: %+v, %v; want a requested wake", stored, err)
			}
		})
	}
}
