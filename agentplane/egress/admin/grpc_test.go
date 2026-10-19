package admin

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/proto/egress_admin"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience    = "test-egress-admin"
	callerNamespace = "test-callers"
	readerToken     = "test-reader-token"
	strangerToken   = "test-stranger-token"
)

// fakeReviews proves a token to the ServiceAccount of the name it maps to, in callerNamespace.
type fakeReviews map[string]string

func (r fakeReviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	name, known := r[review.Spec.Token]
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: known,
		Audiences:     []string{testAudience},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + callerNamespace + ":" + name, UID: "uid-" + name,
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"pod-" + name}, "authentication.kubernetes.io/pod-uid": {"pod-uid-" + name},
			},
		},
	}}, nil
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

func (c *changingDecisions) set(records ...decision.Record) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.records = records
}

// followOnTicks follows as history.Changes does: it calls changed on subscribing and then once per
// tick, and reports on settled after each call returns.
func followOnTicks(ticks <-chan struct{}, settled chan<- struct{}) fakeChanges {
	return func(ctx context.Context, changed func() error) error {
		for {
			err := changed()
			settled <- struct{}{}
			if err != nil {
				return err
			}
			select {
			case <-ticks:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
}

// serveGRPC serves the Service on an in-process gRPC server and returns a client for it. Only
// test-reader is an allowed caller.
func serveGRPC(t *testing.T, decisions Decisions, changes Changes, lease time.Duration) (egressadminpb.EgressAdminClient, *Service) {
	t.Helper()
	resolver, err := auth.NewResolver(
		fakeReviews{readerToken: "test-reader", strangerToken: "test-stranger"}, testAudience, []string{callerNamespace},
	)
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(decisions, changes, resolver, []auth.Account{{Namespace: callerNamespace, Name: "test-reader"}}, lease)
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
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
		Subject: &egressadminpb.Subject{Namespace: namespace, Name: name},
	}}
}

var unauthenticated = &egressadminpb.DecisionFilter{Scope: &egressadminpb.DecisionFilter_Unauthenticated{
	Unauthenticated: &egressadminpb.Unauthenticated{},
}}

func TestEveryRPCAdmitsOnlyAllowedCallersBeforeReadingAnything(t *testing.T) {
	store := &fakeDecisions{}
	changes := fakeChanges(func(context.Context, func() error) error {
		t.Error("history followed before the caller was admitted")
		return nil
	})
	client, _ := serveGRPC(t, store, changes, time.Hour)

	for _, test := range []struct {
		name    string
		ctx     context.Context
		refused codes.Code
	}{
		{"no bearer", context.Background(), codes.Unauthenticated},
		{"bearer no ServiceAccount proved", bearing("test-forged-token"), codes.Unauthenticated},
		{"ServiceAccount not allowed", bearing(strangerToken), codes.PermissionDenied},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := client.ListDecisions(test.ctx, &egressadminpb.ListDecisionsRequest{Filter: unauthenticated})
			if status.Code(err) != test.refused {
				t.Errorf("ListDecisions: %v", err)
			}
			stream, err := client.WatchDecisions(test.ctx, &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
			if err == nil {
				_, err = stream.Recv()
			}
			if status.Code(err) != test.refused {
				t.Errorf("WatchDecisions: %v", err)
			}
		})
	}
	if len(store.asked) != 0 {
		t.Fatalf("history read for a caller that was not admitted: %v", store.asked)
	}
}

func TestListDecisionsReadsTheSelectedSubject(t *testing.T) {
	subject := decision.Subject{Namespace: "test-namespace", Name: "test-workload"}
	binding, policy, address, rule := "test-binding", "test-policy", "203.0.113.7", 2
	refusal := decision.NoRule
	admitted := record(&subject)
	admitted.Binding, admitted.Policy, admitted.Rule, admitted.Address, admitted.Substituted = &binding, &policy, &rule, &address, true
	refused := record(&subject)
	refused.Outcome, refused.Reason = decision.Deny, &refusal
	store := &fakeDecisions{records: []decision.Record{admitted, refused}}
	client, _ := serveGRPC(t, store, nil, time.Hour)

	response, err := client.ListDecisions(bearing(readerToken), &egressadminpb.ListDecisionsRequest{Filter: ofSubject("test-namespace", "test-workload")})
	if err != nil {
		t.Fatal(err)
	}
	want := []*egressadminpb.Decision{
		{
			At: timestamppb.New(started), Method: "GET", Host: "example.test", Port: 443,
			Outcome: egressadminpb.Decision_OUTCOME_ALLOW, Binding: &binding, Policy: &policy,
			Rule: proto.Int32(2), Address: &address, Substituted: true,
		},
		{
			At: timestamppb.New(started), Method: "GET", Host: "example.test", Port: 443,
			Outcome: egressadminpb.Decision_OUTCOME_DENY, Reason: proto.String("no-rule"),
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
	client, _ := serveGRPC(t, store, nil, time.Hour)

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
	ticks, settled := make(chan struct{}), make(chan struct{}, 4)
	client, _ := serveGRPC(t, store, followOnTicks(ticks, settled), time.Hour)

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
	store := &changingDecisions{}
	ticks, settled := make(chan struct{}), make(chan struct{}, 4)
	client, _ := serveGRPC(t, store, followOnTicks(ticks, settled), time.Hour)
	ctx, cancel := context.WithCancel(bearing(readerToken))
	defer cancel()
	stream, err := client.WatchDecisions(ctx, &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}
	receive := func() []*egressadminpb.Decision {
		t.Helper()
		response, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return response.GetDecisions()
	}

	if got := receive(); len(got) != 0 {
		t.Fatalf("subscribing sent %v, want the empty list", got)
	}
	<-settled
	// A commit that adds nothing to this list: the next response must be the change, not a repeat.
	ticks <- struct{}{}
	<-settled
	store.set(record(nil))
	ticks <- struct{}{}
	if got := receive(); len(got) != 1 {
		t.Fatalf("after a change sent %v, want the one decision", got)
	}
}

func TestWatchEndsWhenItsLeaseDoes(t *testing.T) {
	ticks, settled := make(chan struct{}), make(chan struct{}, 4)
	client, _ := serveGRPC(t, &changingDecisions{}, followOnTicks(ticks, settled), 20*time.Millisecond)
	stream, err := client.WatchDecisions(bearing(readerToken), &egressadminpb.WatchDecisionsRequest{Filter: unauthenticated})
	if err != nil {
		t.Fatal(err)
	}

	// The lease may end before the first response is read; either way the stream ends cleanly.
	var ended error
	for ended == nil {
		_, ended = stream.Recv()
	}
	if !errors.Is(ended, io.EOF) {
		t.Fatalf("the lease ended the stream with %v, want a clean end", ended)
	}
}

func TestDrainEndsOpenWatches(t *testing.T) {
	ticks, settled := make(chan struct{}), make(chan struct{}, 4)
	client, service := serveGRPC(t, &changingDecisions{}, followOnTicks(ticks, settled), time.Hour)
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
