package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
)

type callbackWake struct{ calls int }

func (w *callbackWake) WakeForSend(_ context.Context, _, _ string, beforeResume func(int64) error) (bool, error) {
	w.calls++
	if err := beforeResume(41); err != nil {
		return false, err
	}
	return true, nil
}

type executionAccountCatalog struct {
	*growingCatalog
	accountUID string
}

func (c *executionAccountCatalog) GetExecutionAccount(_ context.Context, sandbox SandboxSnapshot) (ServiceAccountIdentity, bool, error) {
	return ServiceAccountIdentity{
		Account: Account{Namespace: sandbox.Namespace, Name: sandbox.Name}, UID: c.accountUID,
	}, true, nil
}

func wakeAttribution(actor, effective ServiceAccountIdentity) *AdmissionAttribution {
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
	actor, effective ServiceAccountIdentity,
) *AdmissionAttribution {
	t.Helper()
	attribution := wakeAttribution(actor, effective)
	if _, err := store.OpenSessionWithIdentity(context.Background(), sessionID, sandboxName, sandboxUID,
		&sessionpb.SessionSpec{}, nil, &effective.Account, &effective, &actor, attribution); err != nil {
		t.Fatal("open wakeable Session:", err)
	}
	return attribution
}

func TestMixedVersionWakeWritersShareCapAndGenerationDedup(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	caller := ServiceAccountIdentity{Account: Account{Namespace: "mixed-writer", Name: "shared-cap"}, UID: "current-uid"}
	const uid = "00000000-0000-4000-8000-000000000091"

	for generation := int64(1); generation <= WakeLimit; generation++ {
		var wait time.Duration
		var err error
		if generation%2 == 1 {
			wait, err = reserveWakeAttemptAsPreviousWriter(ctx, store, caller, uid, generation)
		} else {
			wait, err = store.ReserveWakeAttempt(ctx, caller, uid, generation)
		}
		if err != nil || wait != 0 {
			t.Fatalf("mixed writer reservation %d: wait=%s error=%v", generation, wait, err)
		}
	}
	if wait, err := store.ReserveWakeAttempt(ctx, caller, uid, WakeLimit+1); err != nil || wait == 0 {
		t.Fatalf("current writer did not count legacy rows: wait=%s error=%v", wait, err)
	}
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, caller, uid, 2); err != nil || wait != 0 {
		t.Fatalf("previous writer did not deduplicate current row: wait=%s error=%v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(ctx, caller, uid, 1); err != nil || wait != 0 {
		t.Fatalf("current writer did not deduplicate legacy row: wait=%s error=%v", wait, err)
	}
	var total, legacy, current int
	if err := store.DB.QueryRowContext(ctx, `SELECT count(*),count(*) FILTER (WHERE caller_uid=''),
		count(*) FILTER (WHERE caller_uid=$3) FROM wake_attempt
		WHERE caller_namespace=$1 AND caller_name=$2 AND sandbox_uid=$4`,
		caller.Namespace, caller.Name, caller.UID, uid).Scan(&total, &legacy, &current); err != nil {
		t.Fatal("count mixed-version wake rows:", err)
	}
	if total != WakeLimit || legacy != WakeLimit/2 || current != WakeLimit/2 {
		t.Fatalf("mixed-version wake rows: total=%d legacy=%d current=%d", total, legacy, current)
	}
}

// reserveWakeAttemptAsPreviousWriter models replicas that omit caller_uid and
// read the whole namespace/name budget during the migration rollout.
func reserveWakeAttemptAsPreviousWriter(
	ctx context.Context,
	store *Store,
	caller ServiceAccountIdentity,
	sandboxUID string,
	generation int64,
) (time.Duration, error) {
	var retryAfter time.Duration
	err := transaction(ctx, store.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
			"dispatcher-wake:"+caller.Namespace+":"+caller.Name); err != nil {
			return err
		}
		var now time.Time
		if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
			return err
		}
		windowStart := now.Add(-WakeWindow)
		if _, err := tx.ExecContext(ctx, "DELETE FROM wake_attempt WHERE attempted_at <= $1", windowStart); err != nil {
			return err
		}
		var exists bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND sandbox_uid=$3 AND wake_generation=$4)`,
			caller.Namespace, caller.Name, sandboxUID, generation).Scan(&exists); err != nil {
			return err
		}
		if exists {
			return nil
		}
		var count int
		var oldest sql.NullTime
		if err := tx.QueryRowContext(ctx, `SELECT count(*),min(attempted_at) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND attempted_at>$3`,
			caller.Namespace, caller.Name, windowStart).Scan(&count, &oldest); err != nil {
			return err
		}
		if count >= WakeLimit {
			if !oldest.Valid {
				return errors.New("wake budget full without attempt")
			}
			retryAfter = oldest.Time.Add(WakeWindow).Sub(now)
			if retryAfter < time.Millisecond {
				retryAfter = time.Millisecond
			}
			return nil
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO wake_attempt
			(caller_namespace,caller_name,sandbox_uid,wake_generation,attempted_at)
			VALUES ($1,$2,$3,$4,$5)`, caller.Namespace, caller.Name, sandboxUID, generation, now)
		return err
	})
	return retryAfter, err
}

func TestDeliveryUsesRetainedEffectiveCallerUIDForWakeBudget(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	actor := ServiceAccountIdentity{Account: Account{Namespace: "agentplane", Name: "actions"}, UID: "actions-uid"}
	effective := ServiceAccountIdentity{Account: Account{Namespace: "agentplane", Name: "delegate"}, UID: "delegate-uid"}
	wake := &callbackWake{}
	worker := &DeliveryWorker{Store: store, Wake: wake, Leases: testLease{store: store}}
	worker.init()
	defer worker.queue.ShutDown()

	for i, kind := range []string{"dispatch", "opening"} {
		sessionID := fmt.Sprintf("uid-wake-session-%d", i)
		sandboxName, sandboxUID := fmt.Sprintf("uid-wake-sandbox-%d", i), uuid.NewString()
		attribution := openWakeableSession(t, store, sessionID, sandboxName, sandboxUID, actor, effective)
		sandbox := SandboxSnapshot{
			Name: sandboxName, Namespace: effective.Namespace, UID: sandboxUID,
			Incarnation: "old-pod", Blocker: "sandbox_suspended", WakeOnSend: true,
		}
		owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "wake-lease-"+sessionID, 1, "replica", 0)
		if err != nil || owner == nil {
			t.Fatalf("claim owner for %s: owner=%+v error=%v", kind, owner, err)
		}
		var ready bool
		var prepareErr error
		if kind == "dispatch" {
			caller := CallerIdentity{ServiceAccountIdentity: effective, Attribution: attribution}
			row, err := store.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID,
				input("uid-wake-command", "hello"), &effective.Account, caller)
			if err != nil {
				t.Fatal("enqueue attributed wake command:", err)
			}
			ready, prepareErr = worker.prepareDispatch(ctx, row, sandbox, *owner)
		} else {
			row, err := store.GetOpening(ctx, sessionID)
			if err != nil {
				t.Fatal("read attributed wake opening:", err)
			}
			ready, prepareErr = worker.prepareOpening(ctx, row, sandbox, *owner)
		}
		if prepareErr != nil || ready {
			t.Fatalf("prepare %s: ready=%v error=%v", kind, ready, prepareErr)
		}
		var effectiveRows, actorRows int
		if err := store.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			effective.Namespace, effective.Name, effective.UID, sandboxUID).Scan(&effectiveRows); err != nil {
			t.Fatal("count effective caller wake attempts:", err)
		}
		if err := store.DB.QueryRowContext(ctx, `SELECT count(*) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4`,
			actor.Namespace, actor.Name, actor.UID, sandboxUID).Scan(&actorRows); err != nil {
			t.Fatal("count actor wake attempts:", err)
		}
		if effectiveRows != 1 || actorRows != 0 {
			t.Fatalf("%s wake budget used effective=%d actor=%d rows", kind, effectiveRows, actorRows)
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
	row, err := store.Enqueue(ctx, "legacy-wake-session", "legacy-wake-sandbox", uid,
		input("legacy-wake-command", "hello"), &wakeCaller)
	if err != nil {
		t.Fatal("enqueue legacy row:", err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "legacy-wake-lease", 1, "replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim owner: owner=%+v error=%v", owner, err)
	}
	wake := &callbackWake{}
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
	if stored.Reason != "wake_caller_unknown" || wake.calls != 0 || attempts != 0 {
		t.Fatalf("legacy row gained wake authority: reason=%q wake calls=%d attempts=%d", stored.Reason, wake.calls, attempts)
	}
}

func TestWakePrincipalRequiresEffectiveIdentityAlignment(t *testing.T) {
	actor := ServiceAccountIdentity{Account: Account{Namespace: "agentplane", Name: "actions"}, UID: "actor-uid"}
	effective := ServiceAccountIdentity{Account: Account{Namespace: "agentplane", Name: "delegate"}, UID: "effective-uid"}
	attribution := &AdmissionAttribution{Actor: actor, EffectiveCaller: effective}
	if got := effectiveWakePrincipal(&effective.Account, attribution); got == nil || *got != effective {
		t.Fatalf("effective wake principal=%+v, want %+v", got, effective)
	}
	if got := effectiveWakePrincipal(&actor.Account, attribution); got != nil {
		t.Fatalf("actor account was accepted as effective wake principal: %+v", got)
	}
	withoutUID := *attribution
	withoutUID.EffectiveCaller.UID = ""
	if got := effectiveWakePrincipal(&effective.Account, &withoutUID); got != nil {
		t.Fatalf("unproven effective caller UID was accepted: %+v", got)
	}
}

func TestWakeBudgetRejectsMissingUID(t *testing.T) {
	store := testStore(t, 3)
	if _, err := store.ReserveWakeAttempt(context.Background(), ServiceAccountIdentity{
		Account: Account{Namespace: "agentplane", Name: "missing-uid"},
	}, "00000000-0000-4000-8000-000000000093", 1); err == nil {
		t.Fatal("wake reservation accepted an identity without a UID")
	}
}
