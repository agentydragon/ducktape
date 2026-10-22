package api

import (
	"context"
	"io"
	"sync"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type localAuthority struct {
	mu      sync.Mutex
	account auth.Account
	refused bool
	labeled bool
}

func (a *localAuthority) ResolveCaller(context.Context) (auth.Account, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.refused {
		return auth.Account{}, unauthenticated
	}
	return a.account, nil
}

func (a *localAuthority) Admits(account auth.Account) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.labeled && a.account == account
}

type localCore struct {
	pb.UnimplementedActionServiceServer
	gate     *Gate
	seen     store.ReadPrincipal
	resume   chan struct{}
	finished chan struct{}
}

func (s *localCore) ListActionRequests(ctx context.Context, _ *pb.ListActionRequestsRequest) (*pb.ListActionRequestsResponse, error) {
	s.seen = admitted(ctx).principal
	return nil, store.ErrNotFound
}

func (s *localCore) WatchActionRequest(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	defer close(s.finished)
	if err := send(stream.Context(), s.gate, stream, &pb.WatchActionRequestResponse{}); err != nil {
		return err
	}
	select {
	case <-s.resume:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	return send(stream.Context(), s.gate, stream, &pb.WatchActionRequestResponse{})
}

func localFixture() (*LocalClient, *localAuthority, *localCore) {
	authority := &localAuthority{account: auth.Account{Namespace: "test-namespace", Name: "test-caller"}, labeled: true}
	gate := NewGate(GateOptions{Callers: authority})
	core := &localCore{gate: gate, resume: make(chan struct{}), finished: make(chan struct{})}
	return NewLocalClient(core, gate, authority), authority, core
}

func TestLocalCallsUseCallerAdmissionAndMapDomainErrors(t *testing.T) {
	client, authority, core := localFixture()
	_, err := client.ListActionRequests(t.Context(), &pb.ListActionRequestsRequest{})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("domain error: %v", err)
	}
	want := store.CallerPrincipal{Account: store.ServiceAccount{Namespace: authority.account.Namespace, Name: authority.account.Name}}
	if core.seen != want {
		t.Fatalf("principal: %v; want %v", core.seen, want)
	}
	authority.labeled = false
	_, err = client.ListActionRequests(t.Context(), &pb.ListActionRequestsRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unlabeled: %v", err)
	}
	authority.labeled = true
	core.gate.Drain()
	_, err = client.ListActionRequests(t.Context(), &pb.ListActionRequestsRequest{})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("draining: %v", err)
	}
}

func TestLocalWatchRechecksRevocationRebindingAndCallerLabel(t *testing.T) {
	for _, change := range []string{"revoke", "rebind", "unlabel"} {
		t.Run(change, func(t *testing.T) {
			client, authority, core := localFixture()
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			stream, err := client.WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := stream.Recv(); err != nil {
				t.Fatal(err)
			}
			authority.mu.Lock()
			switch change {
			case "revoke":
				authority.refused = true
			case "rebind":
				authority.account.Name = "test-rebound"
			case "unlabel":
				authority.labeled = false
			}
			authority.mu.Unlock()
			close(core.resume)
			if _, err := stream.Recv(); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("after %s: %v", change, err)
			}
		})
	}
}

func TestLocalWatchCancellationReleasesBlockedSend(t *testing.T) {
	client, _, core := localFixture()
	ctx, cancel := context.WithCancel(t.Context())
	stream, err := client.WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if err != nil {
		t.Fatal(err)
	}
	// The first frame has no receiver, so cancellation must release the servicer's send.
	cancel()
	select {
	case <-core.finished:
	case <-time.After(5 * time.Second):
		t.Fatal("cancelled watch kept its sender blocked")
	}
	if _, err := stream.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled: %v", err)
	}
}

func TestLocalWatchPreservesFinalFrameAndEnd(t *testing.T) {
	client, _, core := localFixture()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	close(core.resume)
	stream, err := client.WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err := stream.Recv(); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Fatalf("end: %v", err)
	}
}
