package store

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"

	"github.com/google/uuid"
)

func TestMixedVersionWakeWritersShareCapAndGenerationDedup(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	caller := identityWakeKey("wake-budget-mixed-version", "shared-cap", "current-uid")
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
		// Retry across versions while there is room and again at the cap.
		if generation == 2 || generation == WakeLimit {
			if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, caller, uid, 2); err != nil || wait != 0 {
				t.Fatalf("previous writer did not deduplicate current row with %d slots taken: %s %v", generation, wait, err)
			}
			if wait, err := store.ReserveWakeAttempt(ctx, caller, uid, 1); err != nil || wait != 0 {
				t.Fatalf("current writer did not deduplicate legacy row with %d slots taken: %s %v", generation, wait, err)
			}
		}
	}
	if wait, err := store.ReserveWakeAttempt(ctx, caller, uid, WakeLimit+1); err != nil || wait == 0 {
		t.Fatalf("current writer did not count legacy rows: wait=%s error=%v", wait, err)
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

func TestWakeBudgetRejectsMissingUID(t *testing.T) {
	store := testStore(t, 3)
	if _, err := store.ReserveWakeAttempt(context.Background(), identityWakeKey("test-wake-namespace", "test-missing-uid", ""),
		"00000000-0000-4000-8000-000000000093", 1); err == nil {
		t.Fatal("wake reservation accepted an identity without a UID")
	}
}

func TestServiceWakeBudgetDoesNotSpendIdentityOrOtherServiceBudget(t *testing.T) {
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
	if wait, err := store.ReserveWakeAttempt(t.Context(), identityWakeKey("test-wake-namespace", "test-wake-identity", "test-wake-identity-uid"), target, 1); err != nil || wait != 0 {
		t.Fatalf("Identity budget: %s %v", wait, err)
	}
	if _, err := store.DB.ExecContext(t.Context(), `UPDATE service_wake_attempt
		SET attempted_at=clock_timestamp() - make_interval(secs => $3)
		WHERE service_spiffe_id=$1 AND sandbox_uid=$2 AND wake_generation=1`,
		key.ServiceSPIFFEID, target, 2*WakeWindow.Seconds()); err != nil {
		t.Fatal(err)
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), key, target, 1); err != nil || wait != 0 {
		t.Fatalf("reuse expired generation: %s %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(t.Context(), key, target, WakeLimit+1); err != nil || wait <= 0 {
		t.Fatalf("expired generation did not consume a fresh slot: %s %v", wait, err)
	}
}
