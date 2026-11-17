package dispatcher

import (
	"context"
	"errors"
	"sync"
	"testing"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func centralActivitySpec(uid string) *sessionpb.SessionSpec {
	return &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "executor", Uid: uid}}}}}
}

func centralActivitySession(id string) *harnesshostpb.Session {
	return &harnesshostpb.Session{
		SessionId: id, ActiveTurnId: uuid.NewString(), PendingCommands: 2,
		Host: &harnesshostpb.Session_CentralSamplerV1{CentralSamplerV1: &harnesshostpb.CentralSamplerSession{Setup: sessionpb.SetupState_SETUP_STATE_RUNNING}},
	}
}

func TestCentralActivityLeaseProjectionAndFencing(t *testing.T) {
	store := testStore(t, 5)
	ctx := admitted(t)
	uid, secondUID, id, second, hosted := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	for _, session := range []struct {
		id, uid string
		spec    *sessionpb.SessionSpec
	}{
		{id, uid, centralActivitySpec(uid)}, {second, secondUID, centralActivitySpec(secondUID)}, {hosted, uid, &sessionpb.SessionSpec{}},
	} {
		if _, err := store.CreateSession(ctx, session.id, "executor", session.uid, session.spec, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	read := func(id string) *dispatcherpb.SessionActivity {
		t.Helper()
		row, err := store.GetSession(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		return describeActivity(row)
	}
	first, err := store.acquireSamplerActivity(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.acquireSamplerActivity(ctx); err == nil {
		t.Fatal("second replica acquired live sampler watch")
	}
	first, err = store.beginActivityEpoch(ctx, first, true)
	if err != nil {
		t.Fatal(err)
	}
	updates := map[string]*harnesshostpb.Session{id: centralActivitySession(id), second: centralActivitySession(second), hosted: centralActivitySession(hosted)}
	// Preserve host fields from newer releases while projecting the observed Session verbatim.
	updates[id].ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	if err := store.recordHostActivity(ctx, first, updates); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{id, second} {
		if !proto.Equal(read(id).GetObserved(), updates[id]) {
			t.Fatalf("central projection %s: %v", id, read(id))
		}
	}
	if read(hosted).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED {
		t.Fatal("sampler wrote hosted Session")
	}
	if err := store.recordEnvironmentDown(ctx, first, func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error) {
		t.Fatal("central watch consulted executor lifecycle")
		return 0, nil
	}); err == nil {
		t.Fatal("central EnvironmentDown accepted")
	}
	if err := store.loseActivityStream(ctx, first); err != nil {
		t.Fatal(err)
	}
	if read(id).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("disconnect retained central status")
	}
	next, err := store.beginActivityEpoch(ctx, first, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.recordHostActivity(ctx, next, updates); err != nil {
		t.Fatal(err)
	}
	if err := store.recordHostActivity(ctx, first, updates); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("old stream write: %v", err)
	}
	if err := store.loseActivityStream(ctx, first); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("old stream invalidation: %v", err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE sandbox_activity_watch SET expires_at=clock_timestamp()-interval '1 second' WHERE central_sampler`); err != nil {
		t.Fatal(err)
	}
	if read(id).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("expired sampler lease read as current")
	}
	takeover, err := store.acquireSamplerActivity(ctx)
	if err != nil {
		t.Fatal(err)
	}
	takeover, err = store.beginActivityEpoch(ctx, takeover, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.recordHostActivity(ctx, next, updates); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("old replica wrote after takeover: %v", err)
	}
	if err := store.recordHostActivity(ctx, takeover, updates); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(read(id).GetObserved(), updates[id]) {
		t.Fatal("takeover did not restore observation")
	}
	// A stored row can never turn executor downtime into central Session downtime.
	activity := read(id)
	activity.Kind = &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: dispatcherpb.SessionActivity_EnvironmentDown_DELETED}}
	data, err := proto.Marshal(activity)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE session SET activity=$2 WHERE session_id=$1`, id, data); err != nil {
		t.Fatal(err)
	}
	if read(id).GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("central row projected EnvironmentDown")
	}
}

type samplerActivityFixture struct{ client *activityClient }

func (c samplerActivityFixture) WatchActivity(ctx context.Context, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	return c.client.WatchActivity(ctx, SandboxSnapshot{}, apply)
}

func TestCentralActivityObserverSnapshotReconnectAndWatch(t *testing.T) {
	store, url := testStoreWithURL(t, 5)
	ctx := admitted(t)
	id, uid := uuid.NewString(), uuid.NewString()
	if _, err := store.CreateSession(ctx, id, "executor", uid, centralActivitySpec(uid), nil, nil); err != nil {
		t.Fatal(err)
	}
	hub := sessionWatchHub(t, url, "central-activity-"+uuid.NewString())
	watch := startSessionWatch(t, sessionWatchService(store, hub), ctx, "")
	if receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED {
		t.Fatal("new central Session is not Unknown")
	}
	receiveCaughtUp(t, watch)
	client := &activityClient{calls: make(chan *activityCall, 8)}
	observers, cancel := context.WithCancel(t.Context())
	var workers sync.WaitGroup
	for range 2 {
		workers.Go(func() {
			(&HostActivityObserver{Store: store, Sampler: samplerActivityFixture{client}}).observeSampler(observers)
		})
	}
	t.Cleanup(func() { cancel(); workers.Wait() })
	first := nextActivityCall(t, client)
	busy := centralActivitySession(id)
	sendActivity(t, first, busy)
	row, err := store.GetSession(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if row.Activity != nil {
		t.Fatal("snapshot published before CaughtUp")
	}
	sendActivity(t, first, nil)
	if got := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetObserved(); !proto.Equal(got, busy) {
		t.Fatalf("watch observed %v", got)
	}
	service := sessionWatchService(store, hub)
	got, err := service.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: id})
	if err != nil || !proto.Equal(got.GetSession().GetStatus().GetActivity().GetObserved(), busy) {
		t.Fatalf("get %v: %v", got, err)
	}
	listed, err := service.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || !proto.Equal(listed.GetSessions()[0].GetStatus().GetActivity().GetObserved(), busy) {
		t.Fatalf("list %v: %v", listed, err)
	}
	select {
	case <-client.calls:
		t.Fatal("two replicas watch one sampler")
	default:
	}
	close(first.disconnect)
	if receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatal("stream loss not published as Unknown")
	}
	next := nextActivityCall(t, client)
	idle := centralActivitySession(id)
	idle.ActiveTurnId, idle.PendingCommands = "", 0
	idle.GetCentralSamplerV1().Setup = sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED
	sendActivity(t, next, idle)
	sendActivity(t, next, nil)
	if got := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetObserved(); !proto.Equal(got, idle) {
		t.Fatalf("reconnect observed %v", got)
	}
	if err := first.apply(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_Upsert{Upsert: busy}}); err != nil {
		t.Fatal(err)
	}
	if err := first.apply(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}}); err == nil {
		t.Fatalf("stale observer callback: %v", err)
	}
	row, err = store.GetSession(ctx, id)
	if err != nil || !proto.Equal(describeActivity(row).GetObserved(), idle) {
		t.Fatalf("stale callback changed current observation: %v", err)
	}
}
