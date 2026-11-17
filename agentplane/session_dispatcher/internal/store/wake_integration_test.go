package store

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestMixedVersionWakeWritersShareCapAndGenerationDedup(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	caller := serviceAccountIdentityFor(auth.Account{Namespace: "wake-budget-mixed-version", Name: "shared-cap"}, "current-uid")
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

func TestWakeAttributionRequiresEffectiveIdentityAlignment(t *testing.T) {
	actor := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "actions"}, "actor-uid")
	effective := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "delegate"}, "effective-uid")
	attribution := &AdmissionAttribution{Actor: actor, EffectiveCaller: effective}
	effectiveAccount := accountOfIdentity(effective)
	actorAccount := accountOfIdentity(actor)
	if err := ValidateAttribution(attribution, &effectiveAccount); err != nil {
		t.Fatalf("valid effective wake identity: %v", err)
	}
	if err := ValidateAttribution(attribution, &actorAccount); err == nil {
		t.Fatal("actor account was accepted as effective wake caller")
	}
	withoutUID := &AdmissionAttribution{
		Actor: proto.Clone(actor).(*identitypb.ServiceAccountIdentity), EffectiveCaller: proto.Clone(effective).(*identitypb.ServiceAccountIdentity),
	}
	withoutUID.EffectiveCaller.Uid = ""
	if err := ValidateAttribution(withoutUID, &effectiveAccount); err == nil {
		t.Fatal("effective caller without a UID was accepted")
	}
}

func TestStoreRejectsMismatchedWakeCaller(t *testing.T) {
	for _, kind := range []string{"dispatch", "opening"} {
		t.Run(kind, func(t *testing.T) {
			ctx := context.Background()
			store := testStore(t, 3)
			actor := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "actions"}, "actor-uid")
			effective := serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "delegate"}, "effective-uid")
			attribution := wakeAttribution(actor, effective)
			sandboxUID := uuid.NewString()
			sessionID := "wake-attribution-" + kind
			const commandID = "wake-attribution-command"
			if kind == "dispatch" {
				openWakeableSession(t, store, sessionID, "sandbox", sandboxUID, actor, effective)
			}
			write := func(wakeCaller *auth.Account) error {
				if kind == "opening" {
					_, err := createSessionForCaller(store, ctx, sessionID, "sandbox", sandboxUID,
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
	if _, err := store.ReserveWakeAttempt(context.Background(), wakeKey(serviceAccountIdentityFor(auth.Account{Namespace: "agentplane", Name: "missing-uid"}, "")),
		"00000000-0000-4000-8000-000000000093", 1); err == nil {
		t.Fatal("wake reservation accepted an identity without a UID")
	}
}

func TestServiceWakeBudgetDoesNotSpendAccountOrOtherServiceBudget(t *testing.T) {
	store := testStore(t, 10)
	target := uuid.NewString()
	key := wake.WakeBudgetKey{ServiceSPIFFEID: peertls.NotificationsURI}
	for generation := int64(1); generation <= WakeLimit; generation++ {
		if wait, err := store.ReserveWakeAttempt(t.Context(), key, target, generation); err != nil || wait != 0 {
			t.Fatalf("reserve %d: %s %v", generation, wait, err)
		}
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), key, target, WakeLimit+1); err != nil || wait <= 0 {
		t.Fatalf("service limit: %s %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), key, target, 1); err != nil || wait != 0 {
		t.Fatalf("retry: %s %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), wake.WakeBudgetKey{ServiceSPIFFEID: peertls.AppURI}, target, 1); err != nil || wait != 0 {
		t.Fatalf("other service: %s %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), wake.WakeBudgetKey{Namespace: "test", Name: "notifications", UID: "real-account-uid"}, target, 1); err != nil || wait != 0 {
		t.Fatalf("account budget: %s %v", wait, err)
	}
}
