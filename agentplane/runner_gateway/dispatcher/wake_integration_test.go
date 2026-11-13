package dispatcher

import (
	"context"
	"fmt"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type callbackWake struct {
	store   *Store
	calls   int
	callers []*lifecycle.ServiceAccountIdentity
}

func (w *callbackWake) RequestWake(
	ctx context.Context,
	target *lifecycle.Target,
	caller lifecycle.WakeCaller,
) (*environmentpb.WakeAssessment, error) {
	w.calls++
	w.callers = append(w.callers, wakeAccount(caller))
	wait, err := w.store.ReserveWakeAttempt(ctx, wakeKey(wakeAccount(caller)), target.GetUid(), 41)
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

type executionAccountCatalog struct {
	*growingCatalog
	accountUID string
}

func (*executionAccountCatalog) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return testExecutionIdentity(), true, nil
}

func (c *executionAccountCatalog) GetExecutionAccount(_ context.Context, sandbox SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return serviceAccountIdentityFor(Account{Namespace: sandbox.Namespace, Name: sandbox.Name}, c.accountUID), true, nil
}

func wakeAttribution(actor, effective *ServiceAccountIdentity) *AdmissionAttribution {
	return &AdmissionAttribution{
		Actor: actor, EffectiveCaller: effective,
		ActorPod: &PodIdentity{Name: "actions-pod", UID: "actions-pod-uid"},
		Action:   &ActionCorrelation{RequestID: uuid.NewString(), ExecutionID: uuid.NewString()},
	}
}

func openWakeableSession(
	t *testing.T,
	store *Store,
	sessionID, sandboxName, sandboxUID string,
	actor, effective *ServiceAccountIdentity,
) *AdmissionAttribution {
	t.Helper()
	attribution := wakeAttribution(actor, effective)
	effectiveAccount := accountOfIdentity(effective)
	if _, err := store.CreateSessionForCaller(context.Background(), sessionID, sandboxName, sandboxUID,
		&sessionpb.SessionSpec{}, nil, &effectiveAccount, testExecutionIdentity(), ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution}); err != nil {
		t.Fatal("open wakeable Session:", err)
	}
	return attribution
}

func TestMixedVersionWakeWritersShareCapAndGenerationDedup(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	caller := serviceAccountIdentityFor(Account{Namespace: "wake-budget-mixed-version", Name: "shared-cap"}, "current-uid")
	const uid = "00000000-0000-4000-8000-000000000091"

	for generation := int64(1); generation <= WakeLimit; generation++ {
		var wait time.Duration
		var err error
		if generation%2 == 1 {
			wait, err = reserveWakeAttemptAsPreviousWriter(ctx, store, caller, uid, generation)
		} else {
			wait, err = store.ReserveWakeAttempt(ctx, wakeKey(caller), uid, generation)
		}
		if err != nil || wait != 0 {
			t.Fatalf("mixed writer reservation %d: wait=%s error=%v", generation, wait, err)
		}
	}
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(caller), uid, WakeLimit+1); err != nil || wait == 0 {
		t.Fatalf("current writer did not count legacy rows: wait=%s error=%v", wait, err)
	}
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, caller, uid, 2); err != nil || wait != 0 {
		t.Fatalf("previous writer did not deduplicate current row: wait=%s error=%v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(caller), uid, 1); err != nil || wait != 0 {
		t.Fatalf("current writer did not deduplicate legacy row: wait=%s error=%v", wait, err)
	}
	var total, legacy, current int
	if err := store.DB.QueryRowContext(ctx, `SELECT count(*),count(*) FILTER (WHERE caller_uid=''),
		count(*) FILTER (WHERE caller_uid=$3) FROM wake_attempt
		WHERE caller_namespace=$1 AND caller_name=$2 AND sandbox_uid=$4`,
		caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), caller.GetUid(), uid).Scan(&total, &legacy, &current); err != nil {
		t.Fatal("count mixed-version wake rows:", err)
	}
	if total != WakeLimit || legacy != WakeLimit/2 || current != WakeLimit/2 {
		t.Fatalf("mixed-version wake rows: total=%d legacy=%d current=%d", total, legacy, current)
	}
}

func TestDeliveryUsesRetainedEffectiveCallerUIDForWakeBudget(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	actor := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "actions"}, "actions-uid")
	effective := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "delegate"}, "delegate-uid")
	wake := &callbackWake{store: store}
	worker := &DeliveryWorker{Store: store, Wake: wake, Leases: testLease{store: store}}
	worker.init()
	defer worker.queue.ShutDown()

	for i, kind := range []string{"dispatch", "opening"} {
		sessionID := fmt.Sprintf("uid-wake-session-%d", i)
		sandboxName, sandboxUID := fmt.Sprintf("uid-wake-sandbox-%d", i), uuid.NewString()
		attribution := openWakeableSession(t, store, sessionID, sandboxName, sandboxUID, actor, effective)
		sandbox := SandboxSnapshot{
			Name: sandboxName, Namespace: effective.GetAccount().GetNamespace(), UID: sandboxUID,
			Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
		}
		owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "wake-lease-"+sessionID, 1, "replica", 0)
		if err != nil || owner == nil {
			t.Fatalf("claim owner for %s: owner=%+v error=%v", kind, owner, err)
		}
		var ready bool
		var prepareErr error
		if kind == "dispatch" {
			caller := ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution}
			effectiveAccount := accountOfIdentity(effective)
			row, err := store.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID,
				input("uid-wake-command", "hello"), &effectiveAccount, caller)
			if err != nil {
				t.Fatal("enqueue attributed wake command:", err)
			}
			ready, prepareErr = worker.prepareDispatch(ctx, row, sandbox, *owner)
		} else {
			row, err := store.GetSession(ctx, sessionID)
			if err != nil {
				t.Fatal("read attributed wake opening:", err)
			}
			ready, prepareErr = worker.prepareSession(ctx, row, sandbox, *owner)
		}
		if prepareErr != nil || ready {
			t.Fatalf("prepare %s: ready=%v error=%v", kind, ready, prepareErr)
		}
		var effectiveRows, actorRows int
		if err := store.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			effective.GetAccount().GetNamespace(), effective.GetAccount().GetName(), effective.GetUid(), sandboxUID).Scan(&effectiveRows); err != nil {
			t.Fatal("count effective caller wake attempts:", err)
		}
		if err := store.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			actor.GetAccount().GetNamespace(), actor.GetAccount().GetName(), actor.GetUid(), sandboxUID).Scan(&actorRows); err != nil {
			t.Fatal("count actor wake attempts:", err)
		}
		if effectiveRows != 1 || actorRows != 0 {
			t.Fatalf("%s wake budget used effective=%d actor=%d rows", kind, effectiveRows, actorRows)
		}
		if got := wake.callers[i]; !sameServiceAccountIdentity(got, effective) {
			t.Fatalf("%s lifecycle caller=%+v, want retained effective identity", kind, got)
		}
	}
	if wake.calls != 2 {
		t.Fatalf("wake service calls=%d, want both queued delivery paths", wake.calls)
	}
}

func TestLegacyWakeCallerWithoutUIDDoesNotWake(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000092"
	wakeCaller := Account{Namespace: "agentplane", Name: "legacy-wake-caller"}
	row, err := enqueueWithSession(t, store, ctx, "legacy-wake-session", "legacy-wake-sandbox", uid,
		input("legacy-wake-command", "hello"), &wakeCaller)
	if err != nil {
		t.Fatal("enqueue legacy row:", err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "legacy-wake-lease", 1, "replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim owner: owner=%+v error=%v", owner, err)
	}
	wake := &callbackWake{store: store}
	worker := &DeliveryWorker{Store: store, Wake: wake, Leases: testLease{store: store}}
	worker.init()
	defer worker.queue.ShutDown()
	_, err = worker.prepareDispatch(ctx, row, SandboxSnapshot{
		Name: "legacy-wake-sandbox", Namespace: wakeCaller.Namespace, UID: uid,
		Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
	}, *owner)
	if err != nil {
		t.Fatal("prepare legacy queued work:", err)
	}
	stored, err := store.GetDispatch(ctx, row.CommandID)
	if err != nil {
		t.Fatal("read legacy queued work:", err)
	}
	var attempts int
	if err := store.DB.QueryRowContext(ctx, "SELECT count(*) FROM wake_attempt WHERE sandbox_uid=$1", uid).Scan(&attempts); err != nil {
		t.Fatal("count legacy wake attempts:", err)
	}
	if stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED ||
		stored.WakeAssessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED ||
		wake.calls != 0 || attempts != 0 {
		t.Fatalf("legacy row gained wake authority: assessment=%+v wake calls=%d attempts=%d",
			stored.WakeAssessment, wake.calls, attempts)
	}
}

func TestWakeAttributionRequiresEffectiveIdentityAlignment(t *testing.T) {
	actor := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "actions"}, "actor-uid")
	effective := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "delegate"}, "effective-uid")
	attribution := &AdmissionAttribution{Actor: actor, EffectiveCaller: effective}
	effectiveAccount := accountOfIdentity(effective)
	actorAccount := accountOfIdentity(actor)
	if err := validateAttribution(attribution, &effectiveAccount); err != nil {
		t.Fatalf("valid effective wake identity: %v", err)
	}
	if err := validateAttribution(attribution, &actorAccount); err == nil {
		t.Fatal("actor account was accepted as effective wake caller")
	}
	withoutUID := &AdmissionAttribution{
		Actor: proto.Clone(actor).(*ServiceAccountIdentity), EffectiveCaller: proto.Clone(effective).(*ServiceAccountIdentity),
	}
	withoutUID.EffectiveCaller.Uid = ""
	if err := validateAttribution(withoutUID, &effectiveAccount); err == nil {
		t.Fatal("effective caller without a UID was accepted")
	}
}

func TestStoreRejectsMismatchedWakeCaller(t *testing.T) {
	for _, kind := range []string{"dispatch", "opening"} {
		t.Run(kind, func(t *testing.T) {
			ctx := context.Background()
			store := testStore(t, 3)
			actor := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "actions"}, "actor-uid")
			effective := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "delegate"}, "effective-uid")
			attribution := wakeAttribution(actor, effective)
			sandboxUID := uuid.NewString()
			sessionID := "wake-attribution-" + kind
			const commandID = "wake-attribution-command"
			if kind == "dispatch" {
				openWakeableSession(t, store, sessionID, "sandbox", sandboxUID, actor, effective)
			}
			write := func(wakeCaller *Account) error {
				if kind == "opening" {
					_, err := store.CreateSessionForCaller(ctx, sessionID, "sandbox", sandboxUID,
						&sessionpb.SessionSpec{}, nil, wakeCaller, testExecutionIdentity(), ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution})
					return err
				}
				_, err := store.EnqueueForCaller(ctx, sessionID, "sandbox", sandboxUID,
					input(commandID, "hello"), wakeCaller,
					ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution})
				return err
			}
			actorAccount := accountOfIdentity(actor)
			effectiveAccount := accountOfIdentity(effective)
			if err := write(&actorAccount); err == nil {
				t.Fatal("store accepted a wake caller different from the effective caller")
			}
			if err := write(&effectiveAccount); err != nil {
				t.Fatal("store rejected matching wake attribution:", err)
			}
			var err error
			if kind == "opening" {
				_, err = store.DB.ExecContext(ctx, "UPDATE session SET wake_caller_name=$1 WHERE session_id=$2", actorAccount.Name, sessionID)
			} else {
				_, err = store.DB.ExecContext(ctx, "UPDATE dispatch SET wake_caller_name=$1 WHERE command_id=$2", actorAccount.Name, commandID)
			}
			if err != nil {
				t.Fatal("modify persisted wake caller:", err)
			}
			if kind == "opening" {
				_, err = store.GetSession(ctx, sessionID)
			} else {
				_, err = store.GetDispatch(ctx, commandID)
			}
			if err == nil {
				t.Fatal("store decoded a mismatched wake caller without an error")
			}
		})
	}
}

func TestWakeBudgetRejectsMissingUID(t *testing.T) {
	store := testStore(t, 3)
	if _, err := store.ReserveWakeAttempt(context.Background(), wakeKey(serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "missing-uid"}, "")),
		"00000000-0000-4000-8000-000000000093", 1); err == nil {
		t.Fatal("wake reservation accepted an identity without a UID")
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
	actor := serviceAccountIdentityFor(Account{Namespace: "agentplane", Name: "actions"}, "actions-uid")
	for name, proof := range map[string]IdentityProof{
		"own workload": IdentitySVID{Pod: &PodIdentity{Name: "sender-pod", UID: "sender-pod-uid"}},
		"acted for": IdentityDelegation{
			Actor: actor, ActorPod: PodIdentity{Name: "actions-pod", UID: "actions-pod-uid"},
			Action: ActionCorrelation{RequestID: uuid.NewString(), ExecutionID: uuid.NewString()},
		},
	} {
		t.Run(name, func(t *testing.T) {
			store := testStore(t, 3)
			sessionID, sandboxName, sandboxUID := "identity-wake-session-"+uuid.NewString()[:8], "identity-wake-sandbox", uuid.NewString()
			spec := &sessionpb.SessionSpec{}
			// Only the Identity's own workload opens the Session here (a seed: in production a privileged
			// caller does); the sender under test then queues input to it.
			if _, err := store.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID,
				testPlacement(spec, sandboxName, sandboxUID), spec, nil, nil, SessionExecution{Identity: identity},
				IdentityCaller{Identity: identity, Proof: IdentitySVID{}}); err != nil {
				t.Fatal("open the Identity-run Session:", err)
			}
			sender := IdentityCaller{Identity: identity, Proof: proof}
			row, err := store.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, input("identity-wake-command-"+uuid.NewString()[:8], "hello"), nil, sender)
			if err != nil {
				t.Fatal("enqueue the Identity sender's input:", err)
			}
			owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "identity-wake-lease", 1, "replica", 0)
			if err != nil || owner == nil {
				t.Fatalf("claim owner: owner=%+v error=%v", owner, err)
			}
			wake := &identityWake{}
			worker := &DeliveryWorker{Store: store, Wake: wake, Leases: testLease{store: store}}
			worker.init()
			defer worker.queue.ShutDown()
			ready, err := worker.prepareDispatch(ctx, row, SandboxSnapshot{
				Name: sandboxName, Namespace: "agentplane", UID: sandboxUID,
				Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
			}, *owner)
			if err != nil || ready {
				t.Fatalf("prepare the Identity sender's input: ready=%v error=%v", ready, err)
			}
			if len(wake.callers) != 1 || !proto.Equal(wake.callers[0].(lifecycle.IdentityWakeCaller).Identity, identity) {
				t.Fatalf("lifecycle asked to wake for %+v, want the sender's Identity %v", wake.callers, identity)
			}
			stored, err := store.GetDispatch(ctx, row.CommandID)
			if err != nil || stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || stored.WakeRequestedAt == nil {
				t.Fatalf("queued input after the wake: %+v, %v; want a requested wake", stored, err)
			}
		})
	}
}
