package identitycache

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// fakePolicy serves each WatchIdentities call from the next scripted session; closing a session ends its stream.
type fakePolicy struct {
	policypb.UnimplementedPolicyServiceServer
	sessions chan chan *policypb.WatchIdentitiesResponse

	mu sync.Mutex
	// stored is what GetIdentity reads, standing in for the API server ahead of the watch.
	stored map[string]*policypb.Identity
	gets   int
}

func (f *fakePolicy) GetIdentity(_ context.Context, request *policypb.GetIdentityRequest) (*policypb.GetIdentityResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.gets++
	identity, ok := f.stored[request.GetName()]
	if !ok {
		return nil, status.Error(codes.NotFound, "fake Identity not found")
	}
	return &policypb.GetIdentityResponse{Identity: identity}, nil
}

func (f *fakePolicy) store(identities ...*policypb.Identity) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, identity := range identities {
		f.stored[identity.GetIdentity().GetName()] = identity
	}
}

func (f *fakePolicy) getCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.gets
}

func (f *fakePolicy) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	select {
	case session := <-f.sessions:
		for frame := range session {
			if err := stream.Send(frame); err != nil {
				return err
			}
		}
		return status.Error(codes.Unavailable, "fake stream ended")
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
}

type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) read() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) advance(by time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(by)
}

func start(t *testing.T, grace time.Duration) (*Cache, *fakePolicy, *clock) {
	t.Helper()
	fake := &fakePolicy{sessions: make(chan chan *policypb.WatchIdentitiesResponse), stored: map[string]*policypb.Identity{}}
	server := grpc.NewServer()
	policypb.RegisterPolicyServiceServer(server, fake)
	conn := grpcserver.ConnectInMemory(t, server)
	cache, err := New(policypb.NewPolicyServiceClient(conn), grace)
	if err != nil {
		t.Fatal(err)
	}
	fakeClock := &clock{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
	cache.now = fakeClock.read
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = cache.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
	return cache, fake, fakeClock
}

func identity(name, uid string) *policypb.Identity {
	return &policypb.Identity{Identity: &policypb.PolicyResourceIdentity{Namespace: "fake-policies", Name: name, Uid: uid}}
}

func snapshot(identities ...*policypb.Identity) []*policypb.WatchIdentitiesResponse {
	return []*policypb.WatchIdentitiesResponse{
		{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: &policypb.IdentityChunk{Identities: identities}}},
		{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}},
	}
}

func upsert(identity *policypb.Identity) *policypb.WatchIdentitiesResponse {
	return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: identity}}
}

func removed(name, uid string) *policypb.WatchIdentitiesResponse {
	return &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{
		Removed: &policypb.PolicyResourceIdentity{Namespace: "fake-policies", Name: name, Uid: uid},
	}}
}

func open(fake *fakePolicy) chan *policypb.WatchIdentitiesResponse {
	session := make(chan *policypb.WatchIdentitiesResponse)
	fake.sessions <- session
	return session
}

func send(session chan *policypb.WatchIdentitiesResponse, frames ...*policypb.WatchIdentitiesResponse) {
	for _, frame := range frames {
		session <- frame
	}
}

// waitFor polls a condition the cache reaches asynchronously.
func waitFor(t *testing.T, what string, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestCacheIsStaleUntilTheFirstSnapshotCompletes(t *testing.T) {
	cache, fake, _ := start(t, time.Minute)
	session := open(fake)
	if _, _, err := cache.Lookup("fake-agent"); !errors.Is(err, ErrStale) {
		t.Fatalf("before any frame: %v, want ErrStale", err)
	}
	send(session, snapshot(identity("fake-agent", "uid-1"))[0])
	if _, _, err := cache.Lookup("fake-agent"); !errors.Is(err, ErrStale) {
		t.Fatalf("before snapshot completes: %v, want ErrStale", err)
	}
	send(session, snapshot()[1])
	waitFor(t, "the snapshot", func() bool { _, ok, _ := cache.Lookup("fake-agent"); return ok })
	if got, _, _ := cache.Lookup("fake-agent"); got != (auth.Identity{Name: "fake-agent", UID: "uid-1"}) {
		t.Fatalf("lookup = %v", got)
	}
}

func TestWaitCurrentReturnsOnceTheFirstSnapshotCompletes(t *testing.T) {
	cache, fake, _ := start(t, time.Minute)
	session := open(fake)
	waited := make(chan error, 1)
	go func() { waited <- cache.WaitCurrent(t.Context()) }()
	send(session, snapshot(identity("fake-agent", "uid-1"))[0])
	select {
	case err := <-waited:
		t.Fatalf("returned before the snapshot completed: %v", err)
	default:
	}
	send(session, snapshot()[1])
	if err := <-waited; err != nil {
		t.Fatal(err)
	}
}

func TestWaitCurrentEndsWithItsContext(t *testing.T) {
	cache, fake, _ := start(t, time.Minute)
	open(fake)
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := cache.WaitCurrent(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v, want context.Canceled", err)
	}
}

func TestCacheTreatsDeletingAndUIDlessIdentitiesAsGone(t *testing.T) {
	cache, fake, _ := start(t, time.Minute)
	deleting := identity("deleting-agent", "uid-deleting")
	deleting.DeletionTimestamp = timestamppb.New(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	session := open(fake)
	send(session, snapshot(identity("live-agent", "uid-live"), deleting, identity("uidless-agent", ""))...)
	waitFor(t, "the snapshot", func() bool { _, err := cache.List(); return err == nil })
	listed, _ := cache.List()
	if len(listed) != 1 || listed[0].Name != "live-agent" {
		t.Fatalf("listed = %v", listed)
	}
	// An upsert that starts deletion removes a live Identity.
	starting := identity("live-agent", "uid-live")
	starting.DeletionTimestamp = deleting.DeletionTimestamp
	send(session, upsert(starting))
	waitFor(t, "the deletion", func() bool { _, ok, _ := cache.Lookup("live-agent"); return !ok })
}

func TestCacheFollowsUpsertsAndRemovesOnlyTheNamedIncarnation(t *testing.T) {
	cache, fake, _ := start(t, time.Minute)
	session := open(fake)
	send(session, snapshot(identity("fake-agent", "uid-old"))...)
	waitFor(t, "the snapshot", func() bool { _, ok, _ := cache.Lookup("fake-agent"); return ok })
	// A same-name replacement can arrive before the old incarnation's removal.
	send(session, upsert(identity("fake-agent", "uid-new")), removed("fake-agent", "uid-old"), upsert(identity("other-agent", "uid-other")))
	waitFor(t, "the upserts", func() bool { _, ok, _ := cache.Lookup("other-agent"); return ok })
	if got, ok, _ := cache.Lookup("fake-agent"); !ok || got.UID != "uid-new" {
		t.Fatalf("after replacement: %v %v, want uid-new", got, ok)
	}
	send(session, removed("fake-agent", "uid-new"))
	waitFor(t, "the removal", func() bool { _, ok, _ := cache.Lookup("fake-agent"); return !ok })
}

func TestCacheAnswersWithinGraceAfterABreakThenRefusesUntilResynced(t *testing.T) {
	cache, fake, fakeClock := start(t, 10*time.Second)
	first := open(fake)
	send(first, snapshot(identity("fake-agent", "uid-1"), identity("deleted-while-away", "uid-2"))...)
	waitFor(t, "the snapshot", func() bool { _, ok, _ := cache.Lookup("fake-agent"); return ok })
	close(first)
	second := open(fake) // The cache has reconnected.
	if _, ok, err := cache.Lookup("fake-agent"); err != nil || !ok {
		t.Fatalf("within grace: %v %v, want the last view", ok, err)
	}
	fakeClock.advance(10 * time.Second)
	if _, _, err := cache.Lookup("fake-agent"); !errors.Is(err, ErrStale) {
		t.Fatalf("after grace: %v, want ErrStale", err)
	}
	send(second, snapshot(identity("fake-agent", "uid-1"))...)
	waitFor(t, "the resync", func() bool { _, err := cache.List(); return err == nil })
	if _, ok, _ := cache.Lookup("deleted-while-away"); ok {
		t.Fatal("an Identity deleted while disconnected survived the resync")
	}
}

// changes returns a check that reports whether Changed closed since the last check.
func changes(cache *Cache) func() bool {
	changed := cache.Changed()
	return func() bool {
		select {
		case <-changed:
			changed = cache.Changed()
			return true
		default:
			return false
		}
	}
}

func TestChangedClosesOnEveryChangeOfTheViewAndWhenItTurnsStale(t *testing.T) {
	cache, fake, _ := start(t, 0)
	changed := changes(cache)
	session := open(fake)
	send(session, snapshot(identity("fake-agent", "uid-1"))...)
	waitFor(t, "the snapshot to signal", changed)
	if _, ok, err := cache.Lookup("fake-agent"); err != nil || !ok {
		t.Fatalf("after the snapshot signalled: %v %v", ok, err)
	}
	send(session, upsert(identity("other-agent", "uid-2")))
	waitFor(t, "the upsert to signal", changed)
	if _, ok, _ := cache.Lookup("other-agent"); !ok {
		t.Fatal("the upsert signalled before it was visible")
	}
	send(session, removed("other-agent", "uid-2"))
	waitFor(t, "the removal to signal", changed)
	if _, ok, _ := cache.Lookup("other-agent"); ok {
		t.Fatal("the removal signalled before it was visible")
	}
	// With no grace, the view turns stale as the stream breaks.
	close(session)
	waitFor(t, "the break to signal", changed)
	if _, err := cache.List(); !errors.Is(err, ErrStale) {
		t.Fatalf("after the break signalled: %v, want ErrStale", err)
	}
}

func TestGetIdentityUIDReadsThroughOnlyWhenTheCurrentViewMisses(t *testing.T) {
	cache, fake, fakeClock := start(t, 10*time.Second)
	session := open(fake)
	if _, _, err := cache.GetIdentityUID(context.Background(), "fake-agent"); !errors.Is(err, ErrStale) || fake.getCount() != 0 {
		t.Fatalf("before the snapshot: %v after %d reads, want ErrStale without reading", err, fake.getCount())
	}
	send(session, snapshot(identity("cached-agent", "uid-cached"))...)
	waitFor(t, "the snapshot", func() bool { _, ok, _ := cache.Lookup("cached-agent"); return ok })
	if uid, ok, err := cache.GetIdentityUID(context.Background(), "cached-agent"); err != nil || !ok || uid != "uid-cached" || fake.getCount() != 0 {
		t.Fatalf("hit: %q %v %v after %d reads, want uid-cached without reading", uid, ok, err, fake.getCount())
	}
	deleting := identity("deleting-agent", "uid-deleting")
	deleting.DeletionTimestamp = timestamppb.New(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	fake.store(identity("just-created-agent", "uid-just-created"), deleting)
	if uid, ok, err := cache.GetIdentityUID(context.Background(), "just-created-agent"); err != nil || !ok || uid != "uid-just-created" {
		t.Fatalf("created ahead of the watch: %q %v %v, want uid-just-created", uid, ok, err)
	}
	for _, name := range []string{"deleting-agent", "absent-agent"} {
		if _, ok, err := cache.GetIdentityUID(context.Background(), name); err != nil || ok {
			t.Fatalf("%s: %v %v, want not found", name, ok, err)
		}
	}
	close(session)
	open(fake)
	fakeClock.advance(10 * time.Second)
	reads := fake.getCount()
	if _, _, err := cache.GetIdentityUID(context.Background(), "just-created-agent"); !errors.Is(err, ErrStale) || fake.getCount() != reads {
		t.Fatalf("after grace: %v, want ErrStale without reading", err)
	}
}
