package dispatcher

import (
	"context"
	"io"
	"sync"
	"testing"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

type activityCatalog struct {
	testCatalog
	mu       sync.Mutex
	down     dispatcherpb.SessionActivity_EnvironmentDown_Cause
	handlers map[int]func(SandboxKey)
	next     int
}

func (c *activityCatalog) GetEnvironment(context.Context, SandboxSnapshot) (*sandboxapi.Sandbox, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.down == dispatcherpb.SessionActivity_EnvironmentDown_DELETED {
		return nil, false, nil
	}
	object := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: c.sandbox.Name, Namespace: c.sandbox.Namespace, UID: types.UID(c.sandbox.UID)}}
	if c.down == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
		object.Status.OperatingMode = sandboxapi.OperatingModeSuspended
		object.Status.StopStage = sandboxapi.StopStageStopped
	}
	return object, true, nil
}

func (c *activityCatalog) WatchSandboxes(enqueue func(SandboxKey)) (func(), error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	id := c.next
	c.next++
	if c.handlers == nil {
		c.handlers = map[int]func(SandboxKey){}
	}
	c.handlers[id] = enqueue
	return func() { c.mu.Lock(); delete(c.handlers, id); c.mu.Unlock() }, nil
}

func (c *activityCatalog) setDown(cause dispatcherpb.SessionActivity_EnvironmentDown_Cause) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.down = cause
	for _, handler := range c.handlers {
		handler(SandboxKey{Name: c.sandbox.Name, UID: c.sandbox.UID})
	}
}

type (
	activityFrame struct {
		frame *harnesshostpb.WatchSessionsResponse
		done  chan error
	}
	activityCall struct {
		apply      func(*harnesshostpb.WatchSessionsResponse) error
		frames     chan activityFrame
		disconnect chan struct{}
		ended      chan struct{}
	}
)
type activityClient struct{ calls chan *activityCall }

func (c *activityClient) WatchActivity(ctx context.Context, _ SandboxSnapshot, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	call := &activityCall{apply: apply, frames: make(chan activityFrame), disconnect: make(chan struct{}), ended: make(chan struct{})}
	defer close(call.ended)
	select {
	case c.calls <- call:
	case <-ctx.Done():
		return ctx.Err()
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-call.disconnect:
			return io.EOF
		case request := <-call.frames:
			err := apply(request.frame)
			request.done <- err
			if err != nil {
				return err
			}
		}
	}
}

func nextActivityCall(t *testing.T, c *activityClient) *activityCall {
	t.Helper()
	select {
	case call := <-c.calls:
		return call
	case <-time.After(10 * time.Second):
		t.Fatal("no HarnessHost watch started")
		return nil
	}
}

func sendActivity(t *testing.T, call *activityCall, session *harnesshostpb.Session) {
	t.Helper()
	frame := &harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}}
	if session != nil {
		frame.Update = &harnesshostpb.WatchSessionsResponse_Upsert{Upsert: session}
	}
	done := make(chan error, 1)
	select {
	case call.frames <- activityFrame{frame, done}:
	case <-time.After(10 * time.Second):
		t.Fatal("watch did not receive frame")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("watch did not apply frame")
	}
}

func TestHostActivityObserverCatchupReconnectAndLifecycle(t *testing.T) {
	store, url := testStoreWithURL(t, 5)
	ctx := admitted(t)
	uid, id := uuid.NewString(), uuid.NewString()
	if _, err := createSession(store, ctx, id, "activity-observer", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	hub := sessionWatchHub(t, url, "activity-observer-"+uuid.NewString())
	watch := startSessionWatch(t, sessionWatchService(store, hub), ctx, "")
	if receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED {
		t.Fatal("new Session is not unknown")
	}
	receiveCaughtUp(t, watch)
	catalog := &activityCatalog{testCatalog: testCatalog{sandbox: SandboxSnapshot{Name: "activity-observer", Namespace: "test", UID: uid}}}
	client := &activityClient{calls: make(chan *activityCall, 8)}
	observers, cancel := context.WithCancel(t.Context())
	var workers sync.WaitGroup
	for range 2 {
		workers.Go(func() {
			observer := &HostActivityObserver{Store: store, Catalog: catalog, Client: client, Hub: hub, Namespace: "test"}
			if err := observer.Run(observers); err != nil && observers.Err() == nil {
				t.Error(err)
			}
		})
	}
	t.Cleanup(func() { cancel(); workers.Wait() })
	first := nextActivityCall(t, client)
	busy := hostSession(id, true)
	busy.ActiveTurnId = uuid.NewString()
	sendActivity(t, first, busy)
	row, err := store.GetSession(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if row.Activity != nil {
		t.Fatal("published snapshot before CaughtUp")
	}
	sendActivity(t, first, nil)
	if got := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetObserved(); got.GetActiveTurnId() != busy.ActiveTurnId {
		t.Fatalf("observed %v", got)
	}
	select {
	case <-client.calls:
		t.Fatal("two replicas watch one live Sandbox")
	default:
	}
	close(first.disconnect)
	if got := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetUnknown().GetReason(); got != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
		t.Fatalf("disconnect reason %s", got)
	}
	second := nextActivityCall(t, client)
	sendActivity(t, second, hostSession(id, false))
	sendActivity(t, second, nil)
	if got := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity().GetObserved().GetEnvironmentHosted().GetHarness().GetState(); got != harnesshostpb.HarnessAttachment_STATE_DETACHED {
		t.Fatalf("reconnected state %s", got)
	}
	// Replaying a callback retained by a dead stream cannot overwrite the new observation.
	if err := first.apply(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_Upsert{Upsert: busy}}); err != nil {
		t.Fatal(err)
	}
	if err := first.apply(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}}); err == nil {
		t.Fatal("stale callback committed")
	}
	catalog.setDown(dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED)
	for {
		activity := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity()
		if activity.GetEnvironmentDown().GetCause() == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
			break
		}
		if activity.GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
			t.Fatalf("unexpected suspend transition %v", activity)
		}
	}
	createdWhileSuspended := uuid.NewString()
	if _, err := createSession(store, ctx, createdWhileSuspended, "activity-observer", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	for {
		session := receiveSessionWatchFrame(t, watch).GetSession()
		if session.GetSessionId() == createdWhileSuspended && session.GetStatus().GetActivity().GetEnvironmentDown().GetCause() == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED {
			break
		}
	}
	catalog.setDown(dispatcherpb.SessionActivity_EnvironmentDown_DELETED)
	for {
		activity := receiveSessionWatchFrame(t, watch).GetSession().GetStatus().GetActivity()
		if activity.GetEnvironmentDown().GetCause() == dispatcherpb.SessionActivity_EnvironmentDown_DELETED {
			break
		}
		if activity.GetUnknown().GetReason() != dispatcherpb.SessionActivity_Unknown_STREAM_LOST {
			t.Fatalf("unexpected delete transition %v", activity)
		}
	}
}
