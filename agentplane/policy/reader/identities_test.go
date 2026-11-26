package reader

import (
	"context"
	"slices"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// fakeIdentities serves each WatchIdentities call from the next session a test opens; closing a
// session ends its stream.
type fakeIdentities struct {
	policypb.UnimplementedPolicyServiceServer
	sessions chan chan *policypb.WatchIdentitiesResponse
}

func (f *fakeIdentities) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	select {
	case session := <-f.sessions:
		for frame := range session {
			if err := stream.Send(frame); err != nil {
				return err
			}
		}
		return status.Error(codes.Unavailable, "test stream ended")
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
}

func (f *fakeIdentities) open() chan *policypb.WatchIdentitiesResponse {
	session := make(chan *policypb.WatchIdentitiesResponse)
	f.sessions <- session
	return session
}

// startView runs an Identity view over a fake PolicyService until the test ends. With no grace the
// view turns stale as soon as its stream breaks.
func startView(t *testing.T) (*identitycache.Cache, *fakeIdentities) {
	t.Helper()
	fake := &fakeIdentities{sessions: make(chan chan *policypb.WatchIdentitiesResponse)}
	server := grpc.NewServer()
	policypb.RegisterPolicyServiceServer(server, fake)
	view, err := identitycache.New(policypb.NewPolicyServiceClient(grpcserver.ConnectInMemory(t, server)), 0)
	if err != nil {
		t.Fatal(err)
	}
	kubeapiserver.RunUntilCleanup(t, func(ctx context.Context) { _ = view.Run(ctx) })
	return view, fake
}

func viewed(name, uid string) *policypb.Identity {
	return &policypb.Identity{Identity: &policypb.PolicyResourceIdentity{Name: name, Uid: uid}}
}

func snapshotOf(identities ...*policypb.Identity) []*policypb.WatchIdentitiesResponse {
	return []*policypb.WatchIdentitiesResponse{
		{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: &policypb.IdentityChunk{Identities: identities}}},
		{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}},
	}
}

func sendFrames(session chan *policypb.WatchIdentitiesResponse, frames ...*policypb.WatchIdentitiesResponse) {
	for _, frame := range frames {
		session <- frame
	}
}

// identityGranted creates a set and a binding of the Identity named agent to it.
func (f *fixture) identityGranted(agent string) {
	f.Create(f.policySet("test-set", testRule()))
	grant := f.policyBinding("test-identity-grant", "test-set")
	grant.Spec.Subject = policyapi.Subject{Identity: &policyapi.IdentityRef{Name: agent}}
	f.Create(grant)
}

func TestACachedIdentityViewFeedsTheIndex(t *testing.T) {
	f := newFixture(t)
	f.identityGranted("test-agent")
	view, fake := startView(t)
	f.startWith(CachedIdentities{View: view})
	session := fake.open()
	f.eventually("the informers to list", func() bool { return f.index.Resolve([]string{"test-set"}).Rules() == 1 })
	if got := f.health.Status(); got.Synced {
		t.Fatalf("synced before the Identity view answered: %+v", got)
	}

	first := Identity{Name: "test-agent", UID: "test-uid-1"}
	sendFrames(session, snapshotOf(viewed(first.Name, string(first.UID)))...)
	f.eventually("the snapshot to grant", func() bool {
		return f.health.Available() && slices.Equal(grantedBindings(f.index.Grants(first)), []string{"test-identity-grant"})
	})

	// A same-named successor replaces the first incarnation.
	second := Identity{Name: "test-agent", UID: "test-uid-2"}
	sendFrames(session, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: viewed(second.Name, string(second.UID))}})
	f.eventually("the successor to grant", func() bool {
		return slices.Equal(grantedBindings(f.index.Grants(second)), []string{"test-identity-grant"})
	})
	if got := f.index.Grants(first); len(got) != 0 {
		t.Errorf("the replaced incarnation still holds %v", grantedBindings(got))
	}

	sendFrames(session, &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{
		Removed: &policypb.PolicyResourceIdentity{Name: second.Name, Uid: string(second.UID)},
	}})
	f.eventually("the removed Identity to grant nothing", func() bool { return len(f.index.Grants(second)) == 0 })
}

func TestAStaleIdentityViewMakesTheRulesUnavailableAndGrantsNoIdentity(t *testing.T) {
	f := newFixture(t)
	f.identityGranted("test-agent")
	view, fake := startView(t)
	f.startWith(CachedIdentities{View: view})
	agent := Identity{Name: "test-agent", UID: "test-uid"}
	session := fake.open()
	sendFrames(session, snapshotOf(viewed(agent.Name, string(agent.UID)))...)
	f.eventually("the first sync", func() bool { return f.health.Available() && len(f.index.Grants(agent)) == 1 })

	close(session)
	f.eventually("the view to turn unavailable", func() bool { return !f.health.Available() })
	failure, failing := f.health.Status().Failing[Identities]
	if !failing || failure.Error != identitycache.ErrStale.Error() {
		t.Errorf("failing = %+v, want identities failing with ErrStale", f.health.Status().Failing)
	}
	if got := f.index.Grants(agent); len(got) != 0 {
		t.Errorf("a stale view granted %v", grantedBindings(got))
	}

	sendFrames(fake.open(), snapshotOf(viewed(agent.Name, string(agent.UID)))...)
	f.eventually("the resynced view to grant again", func() bool { return f.health.Available() && len(f.index.Grants(agent)) == 1 })
}

func TestWatchRulesRefusesACachedSourceWithoutAView(t *testing.T) {
	err := WatchRules(NewWatches(apiConfig, watchhealth.New()), NewIndex(testRuleType, parseHosts), "test-unused", testCondition, CachedIdentities{})
	if err == nil {
		t.Fatal("WatchRules accepted CachedIdentities with no View")
	}
}
