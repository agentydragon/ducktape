package dispatcher

import (
	"context"
	"errors"
	"testing"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func hostSession(id string, attached bool) *harnesshostpb.Session {
	state := harnesshostpb.HarnessAttachment_STATE_DETACHED
	if attached {
		state = harnesshostpb.HarnessAttachment_STATE_ATTACHED
	}
	return &harnesshostpb.Session{SessionId: id, Host: &harnesshostpb.Session_EnvironmentHosted{EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{Harness: &harnesshostpb.HarnessAttachment{State: state}}}}
}

func TestActivityLeaseAndStreamEpochFence(t *testing.T) {
	store := testStore(t, 5)
	ctx := admitted(t)
	uid := uuid.NewString()
	attached, dormant, missing, central := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	for _, id := range []string{attached, dormant, missing} {
		if _, err := createSession(store, ctx, id, "shared-host", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	centralSpec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "shared-host", Uid: uid}}}}}
	if _, err := createSession(store, ctx, central, "shared-host", uid, centralSpec, nil, nil); err != nil {
		t.Fatal(err)
	}
	first, err := store.AcquireActivity(ctx, uid)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcquireActivity(ctx, uid); err == nil {
		t.Fatal("second replica acquired a live lease")
	}
	first, err = store.BeginActivityEpoch(ctx, first, true)
	if err != nil {
		t.Fatal(err)
	}
	busy := hostSession(attached, true)
	busy.ActiveTurnId = uuid.NewString()
	busy.PendingCommands = 2
	idle := hostSession(dormant, false)
	updates := map[string]*harnesshostpb.Session{attached: busy, dormant: idle, central: busy}
	if err := store.RecordHostActivity(ctx, first, updates); err != nil {
		t.Fatal(err)
	}
	read := func(id string) *dispatcherpb.SessionActivity {
		t.Helper()
		row, err := store.GetSession(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		session, err := DescribeSession(row)
		if err != nil {
			t.Fatal(err)
		}
		return session.Status.Activity
	}
	for id, want := range map[string]*harnesshostpb.Session{attached: busy, dormant: idle} {
		if got := read(id).GetObserved(); !proto.Equal(got, want) {
			t.Fatalf("%s observed %v want %v", id, got, want)
		}
	}
	for _, id := range []string{missing, central} {
		if read(id).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED {
			t.Fatalf("missing/central %s is not unknown", id)
		}
	}
	if err := store.LoseActivityStream(ctx, first); err != nil {
		t.Fatal(err)
	}
	if read(attached).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("disconnect retained observation")
	}
	if err := store.RecordHostActivity(ctx, first, updates); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("late callback after disconnect: %v", err)
	}
	second, err := store.BeginActivityEpoch(ctx, first, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RecordHostActivity(ctx, second, updates); err != nil {
		t.Fatal(err)
	}
	if err := store.LoseActivityStream(ctx, first); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("old stream invalidated new stream: %v", err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE sandbox_activity_watch SET expires_at=clock_timestamp()-interval '1 second' WHERE sandbox_uid=$1`, uid); err != nil {
		t.Fatal(err)
	}
	if read(attached).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("expired observation did not fail closed on read")
	}
	takeover, err := store.AcquireActivity(ctx, uid)
	if err != nil {
		t.Fatal(err)
	}
	takeover, err = store.BeginActivityEpoch(ctx, takeover, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RecordHostActivity(ctx, second, updates); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("old replica overwrote takeover: %v", err)
	}
	for _, cause := range []dispatcherpb.SessionActivity_EnvironmentDown_Cause{dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED, dispatcherpb.SessionActivity_EnvironmentDown_DELETED} {
		if err := store.RecordEnvironmentDown(ctx, takeover, func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) { return cause, nil }); err != nil {
			t.Fatal(err)
		}
		if cause == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
			if _, err := store.DB.ExecContext(ctx, `UPDATE sandbox_activity_watch SET expires_at=clock_timestamp()-interval '1 second' WHERE sandbox_uid=$1`, uid); err != nil {
				t.Fatal(err)
			}
			if read(attached).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
				t.Fatal("expired suspended observation did not fail closed")
			}
			if _, err := store.DB.ExecContext(ctx, `UPDATE sandbox_activity_watch SET expires_at=clock_timestamp()+interval '30 seconds' WHERE sandbox_uid=$1`, uid); err != nil {
				t.Fatal(err)
			}
		}
		for _, id := range []string{attached, dormant, missing} {
			if read(id).GetEnvironmentDown().GetCause() != cause {
				t.Fatalf("%s lacks down cause %s", id, cause)
			}
		}
	}
	before, err := store.GetSession(ctx, attached)
	if err != nil {
		t.Fatal(err)
	}
	repeated, err := store.BeginActivityEpoch(ctx, takeover, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RecordEnvironmentDown(ctx, repeated, func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) {
		return dispatcherpb.SessionActivity_EnvironmentDown_DELETED, nil
	}); err != nil {
		t.Fatal(err)
	}
	after, err := store.GetSession(ctx, attached)
	if err != nil {
		t.Fatal(err)
	}
	if before.Version != after.Version {
		t.Fatalf("identical down reconciliation advanced version %d -> %d", before.Version, after.Version)
	}
	if read(central).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED {
		t.Fatal("host observer wrote central Session")
	}
	if err := store.RecordEnvironmentDown(ctx, repeated, func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) { return 0, nil }); err == nil {
		t.Fatal("unconfirmed EnvironmentDown accepted")
	}
}
