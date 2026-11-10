package admin

import (
	"context"
	"errors"
	"io"
	"maps"
	"net"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testAudience    = "test-egress-admin"
	callerNamespace = "test-callers"
)

var (
	api *kubeapiserver.Server
	// readerToken proves test-reader, an allowed caller; strangerToken proves test-stranger, which is
	// not.
	readerToken, strangerToken string
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		api = server
		ctx := context.Background()
		if readerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: callerNamespace, Name: "test-reader"}, testAudience); err != nil {
			return err
		}
		strangerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: callerNamespace, Name: "test-stranger"}, testAudience)
		return err
	}))
}

// changingDecisions serves the records it was last given.
type changingDecisions struct {
	mu      sync.Mutex
	records []decision.Record
}

func (c *changingDecisions) Recent(context.Context, *decision.Subject) ([]decision.Record, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.records, nil
}

func (c *changingDecisions) ByEventIDs(_ context.Context, eventIDs []uuid.UUID) ([]decision.Record, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var records []decision.Record
	for _, record := range c.records {
		if slices.Contains(eventIDs, record.EventID) {
			records = append(records, record)
		}
	}
	return records, nil
}

func (*changingDecisions) Readable(context.Context) error { return nil }

func (*changingDecisions) Window() history.Window {
	return history.Window{Retention: 24 * time.Hour, Capacity: 2}
}

func (c *changingDecisions) set(records ...decision.Record) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.records = records
}

// followOnTicks follows as history.Changes does: it calls changed on subscribing and then once per
// tick, and reports on settled after each call returns.
func followOnTicks(ticks <-chan []uuid.UUID, settled chan<- struct{}) fakeChanges {
	return func(ctx context.Context, _ *decision.Subject, changed func(history.Change) error) error {
		if err := changed(history.Initial{}); err != nil {
			settled <- struct{}{}
			return err
		}
		settled <- struct{}{}
		for {
			select {
			case eventIDs := <-ticks:
				if err := changed(history.Events{EventIDs: eventIDs}); err != nil {
					settled <- struct{}{}
					return err
				}
				settled <- struct{}{}
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
}

// serveGRPC serves a Service reading what options holds on an in-process gRPC server and returns a
// client for it. Only test-reader is an allowed caller.
func serveGRPC(t *testing.T, options ServiceOptions) (egressadminpb.EgressAdminClient, *Service) {
	t.Helper()
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{callerNamespace})
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(options)
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(Methods(resolver, []auth.Account{{Namespace: callerNamespace, Name: "test-reader"}}).ServerOptions(0)...)
	egressadminpb.RegisterEgressAdminServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///egress-admin-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return egressadminpb.NewEgressAdminClient(connection), service
}

func bearing(token string) context.Context {
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+token))
}

func ofSubject(namespace, name string) *egressadminpb.DecisionFilter {
	return &egressadminpb.DecisionFilter{Scope: &egressadminpb.DecisionFilter_Subject{
		Subject: &identitypb.ServiceAccount{Namespace: namespace, Name: name},
	}}
}

var unauthenticated = &egressadminpb.DecisionFilter{Scope: &egressadminpb.DecisionFilter_Unauthenticated{
	Unauthenticated: &egressadminpb.Unauthenticated{},
}}

func TestEveryRPCAdmitsOnlyAllowedCallers(t *testing.T) {
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{callerNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver, []auth.Account{{Namespace: callerNamespace, Name: "test-reader"}})
	register := func(server *grpc.Server) {
		egressadminpb.RegisterEgressAdminServer(server, egressadminpb.UnimplementedEgressAdminServer{})
	}
	connection := grpcmethods.Serve(t, methods, register)
	for _, method := range grpcmethods.Of(egressadminpb.EgressAdmin_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			// The stand-in servicer answers Unimplemented to a call the policy admitted.
			for _, test := range []struct {
				name string
				ctx  context.Context
				want codes.Code
			}{
				{"no bearer", context.Background(), codes.Unauthenticated},
				{"bearer no ServiceAccount proved", bearing("test-forged-token"), codes.Unauthenticated},
				{"ServiceAccount not allowed", bearing(strangerToken), codes.PermissionDenied},
				{"allowed", bearing(readerToken), codes.Unimplemented},
			} {
				if got := status.Code(method.Call(test.ctx, connection)); got != test.want {
					t.Errorf("%s: %v, want %v", test.name, got, test.want)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(readerToken), grpcmethods.Serve(t, unlisted, register))); got != codes.PermissionDenied {
				t.Errorf("unlisted, an allowed caller got %v", got)
			}
		})
	}
}

func TestListDecisionsReadsTheSelectedSubject(t *testing.T) {
	subject := decision.Subject{Namespace: "test-namespace", Name: "test-workload"}
	binding, policy, address, rule := "test-binding", "test-policy", "203.0.113.7", 2
	refusal := decision.NoRule
	admitted := record(&subject)
	admitted.Binding, admitted.Policy, admitted.Rule, admitted.Address, admitted.Substituted = &binding, &policy, &rule, &address, true
	admitted.Impersonated = &decision.Subject{Namespace: "test-accounts", Name: "test-account"}
	refused := record(&subject)
	refused.Outcome, refused.Reason = decision.Deny, &refusal
	store := &fakeDecisions{records: []decision.Record{admitted, refused}}
	client, _ := serveGRPC(t, ServiceOptions{Decisions: store})

	response, err := client.ListDecisions(bearing(readerToken), &egressadminpb.ListDecisionsRequest{Filter: ofSubject("test-namespace", "test-workload")})
	if err != nil {
		t.Fatal(err)
	}
	want := []*egressadminpb.Decision{
		{
			EventId: admitted.EventID.String(), At: timestamppb.New(started), Method: "GET", Host: "example.test", Port: 443,
			Outcome: egressadminpb.Decision_OUTCOME_ALLOW, Binding: &binding, Policy: &policy,
			Rule: proto.Int32(2), Address: &address, Substituted: true,
			Impersonated: &identitypb.ServiceAccount{Namespace: "test-accounts", Name: "test-account"},
		},
		{
			EventId: refused.EventID.String(), At: timestamppb.New(started), Method: "GET", Host: "example.test", Port: 443,
			Outcome: egressadminpb.Decision_OUTCOME_DENY, Reason: egressadminpb.Decision_REASON_NO_RULE.Enum(),
		},
	}
	if len(response.GetDecisions()) != len(want) {
		t.Fatalf("got %v, want %v", response.GetDecisions(), want)
	}
	for i := range want {
		if !proto.Equal(response.GetDecisions()[i], want[i]) {
			t.Errorf("decision %d: got %v, want %v", i, response.GetDecisions()[i], want[i])
		}
	}

	if _, err := client.ListDecisions(bearing(readerToken), &egressadminpb.ListDecisionsRequest{Filter: unauthenticated}); err != nil {
		t.Fatal(err)
	}
	if len(store.asked) != 2 || *store.asked[0] != subject || store.asked[1] != nil {
		t.Fatalf("asked for %v", store.asked)
	}
}

func TestFilterMustSelectAWholeSubjectOrTheUnauthenticated(t *testing.T) {
	store := &fakeDecisions{}
	client, _ := serveGRPC(t, ServiceOptions{Decisions: store})

	for name, filter := range map[string]*egressadminpb.DecisionFilter{
		"absent":       nil,
		"empty":        {},
		"no name":      ofSubject("test-namespace", ""),
		"no namespace": ofSubject("", "test-workload"),
		"neither":      ofSubject("", ""),
	} {
		_, err := client.ListDecisions(bearing(readerToken), &egressadminpb.ListDecisionsRequest{Filter: filter})
		if status.Code(err) != codes.InvalidArgument {
			t.Errorf("%s: %v", name, err)
		}
	}
	if len(store.asked) != 0 {
		t.Fatalf("history read for a filter that selects nothing: %v", store.asked)
	}
}

func TestHistoryFailureIsExplicitAndPrivate(t *testing.T) {
	store := &fakeDecisions{err: errors.New("test private exception must not escape")}
	ticks, settled := make(chan []uuid.UUID), make(chan struct{}, 4)
	client, _ := serveGRPC(t, ServiceOptions{Decisions: store, Changes: followOnTicks(ticks, settled)})

	_, listed := client.ListDecisions(bearing(readerToken), &egressadminpb.ListDecisionsRequest{Filter: unauthenticated})
	stream, err := client.WatchDecisions(bearing(readerToken), &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}
	_, watched := stream.Recv()
	for name, err := range map[string]error{"ListDecisions": listed, "WatchDecisions": watched} {
		if status.Code(err) != codes.Unavailable || strings.Contains(status.Convert(err).Message(), "test private exception") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestWatchSendsTheListOnSubscribingAndOnlyWhenItChanges(t *testing.T) {
	older, newer := record(nil), record(nil)
	older.At, newer.At = time.Now().Add(-3*time.Minute), time.Now().Add(-time.Minute)
	store := &changingDecisions{records: []decision.Record{older, newer}}
	ticks, settled := make(chan []uuid.UUID), make(chan struct{}, 4)
	client, _ := serveGRPC(t, ServiceOptions{Decisions: store, Changes: followOnTicks(ticks, settled)})
	ctx, cancel := context.WithTimeout(bearing(readerToken), 10*time.Second)
	defer cancel()
	stream, err := client.WatchDecisions(ctx, &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || first.GetSnapshot() == nil || len(first.GetSnapshot().GetDecisions()) != 2 {
		t.Fatalf("first frame = %v, %v; want the two-record snapshot", first, err)
	}
	<-settled
	// An unchanged read produces no delta.
	ticks <- nil
	<-settled
	late := record(nil)
	late.At = older.At.Add(time.Minute)
	store.set(older, newer, late)
	ticks <- []uuid.UUID{late.EventID}
	frame, err := stream.Recv()
	if err != nil || len(frame.GetDelta().GetAdded()) != 1 || frame.GetDelta().GetAdded()[0].GetEventId() != late.EventID.String() ||
		len(frame.GetDelta().GetRemovedEventIds()) != 1 || frame.GetDelta().GetRemovedEventIds()[0] != older.EventID.String() {
		t.Fatalf("late commit and capacity eviction frame = %v, %v", frame, err)
	}
}

func TestDrainEndsOpenWatches(t *testing.T) {
	ticks, settled := make(chan []uuid.UUID), make(chan struct{}, 4)
	client, service := serveGRPC(t, ServiceOptions{Decisions: &changingDecisions{}, Changes: followOnTicks(ticks, settled)})
	stream, err := client.WatchDecisions(bearing(readerToken), &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	service.Drain()
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("draining ended the stream with %v, want a clean end", err)
	}
}

type blockedDecisionsWatch struct {
	grpc.ServerStream
	ctx          context.Context
	started      chan struct{}
	finished     chan struct{}
	release      <-chan struct{}
	startOnce    sync.Once
	finishedOnce sync.Once
}

func (w *blockedDecisionsWatch) Context() context.Context { return w.ctx }

func (w *blockedDecisionsWatch) Send(*egressadminpb.WatchDecisionsResponse) error {
	w.startOnce.Do(func() { close(w.started) })
	<-w.release
	w.finishedOnce.Do(func() { close(w.finished) })
	return nil
}

func TestWatchDecisionsEndsBlockedWriteOnDrainOrCancellation(t *testing.T) {
	for _, tc := range []struct {
		name string
		end  string
		want codes.Code
	}{
		{name: "drain", end: "drain", want: codes.OK},
		{name: "caller cancellation", end: "cancel", want: codes.Canceled},
	} {
		t.Run(tc.name, func(t *testing.T) {
			changes := fakeChanges(func(_ context.Context, _ *decision.Subject, changed func(history.Change) error) error {
				return changed(history.Initial{})
			})
			_, service := serveGRPC(t, ServiceOptions{Decisions: &changingDecisions{}, Changes: changes})
			base := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+readerToken))
			ctx, cancel := context.WithCancel(base)
			defer cancel()
			release := make(chan struct{})
			stream := &blockedDecisionsWatch{
				ctx: ctx, started: make(chan struct{}), finished: make(chan struct{}), release: release,
			}
			done := make(chan error, 1)
			go func() {
				done <- service.watchDecisions(&egressadminpb.WatchDecisionsRequest{Filter: unauthenticated}, stream, func(context.Context) error { return nil })
			}()
			select {
			case <-stream.started:
			case <-time.After(5 * time.Second):
				t.Fatal("WatchDecisions did not reach its initial write")
			}

			switch tc.end {
			case "drain":
				service.Drain()
			case "cancel":
				cancel()
			}
			select {
			case err := <-done:
				if got := status.Code(err); got != tc.want {
					t.Fatalf("WatchDecisions() error = %v, code %s; want %s", err, got, tc.want)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("drain or cancellation did not return the handler")
			}

			close(release)
			select {
			case <-stream.finished:
			case <-time.After(5 * time.Second):
				t.Fatal("the blocked Send did not finish after release")
			}
		})
	}
}

// recordingDecisionsWatch receives frames without a transport, for exercising the handler's
// authorization and feed lifecycle independently of the token review cache.
type recordingDecisionsWatch struct {
	grpc.ServerStream
	ctx    context.Context
	frames chan *egressadminpb.WatchDecisionsResponse
}

func (w *recordingDecisionsWatch) Context() context.Context { return w.ctx }
func (w *recordingDecisionsWatch) Send(frame *egressadminpb.WatchDecisionsResponse) error {
	w.frames <- frame
	return nil
}

func TestWatchRevalidatesWhileFeedIsQuiet(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	changes := fakeChanges(func(ctx context.Context, _ *decision.Subject, changed func(history.Change) error) error {
		if err := changed(history.Initial{}); err != nil {
			return err
		}
		<-ctx.Done()
		return ctx.Err()
	})
	service := NewService(ServiceOptions{Decisions: &changingDecisions{}, Changes: changes})
	stream := &recordingDecisionsWatch{ctx: ctx, frames: make(chan *egressadminpb.WatchDecisionsResponse, 1)}
	var reviews int
	done := make(chan error, 1)
	go func() {
		done <- service.watchDecisions(&egressadminpb.WatchDecisionsRequest{Filter: unauthenticated}, stream, func(context.Context) error {
			reviews++
			if reviews > 1 {
				return status.Error(codes.PermissionDenied, "revoked")
			}
			return nil
		})
	}()
	select {
	case frame := <-stream.frames:
		if frame.GetSnapshot() == nil {
			t.Fatalf("first frame = %v, want snapshot", frame)
		}
	case <-time.After(time.Second):
		t.Fatal("no initial snapshot")
	}
	select {
	case err := <-done:
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("got %v, want permission denied", err)
		}
	case <-time.After(7 * time.Second):
		t.Fatal("quiet watch did not revalidate")
	}
}

func TestWatchFailsWhenHistoryFeedEnds(t *testing.T) {
	changes := fakeChanges(func(_ context.Context, _ *decision.Subject, changed func(history.Change) error) error {
		return changed(history.Initial{})
	})
	client, _ := serveGRPC(t, ServiceOptions{Decisions: &changingDecisions{}, Changes: changes})
	stream, err := client.WatchDecisions(bearing(readerToken), &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || first.GetSnapshot() == nil {
		t.Fatalf("first frame = %v, error %v", first, err)
	}
	_, err = stream.Recv()
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("feed ended with %v, want unavailable", err)
	}
}

func TestReasonOfNamesStoredReasonsAndAnUnnamedOneAsUnknown(t *testing.T) {
	for reason, want := range map[decision.Reason]egressadminpb.Decision_Reason{
		decision.NoRule:                 egressadminpb.Decision_REASON_NO_RULE,
		decision.ImpersonationForbidden: egressadminpb.Decision_REASON_IMPERSONATION_FORBIDDEN,
		"test-unnamed-reason":           egressadminpb.Decision_REASON_UNKNOWN,
	} {
		if got := reasonOf(reason); got != want {
			t.Errorf("reasonOf(%q) = %v, want %v", reason, got, want)
		}
	}
}
