package actions

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testNamespace  = "test-namespace"
	testSandbox    = "test-sandbox"
	testSandboxUID = "test-uid"
)

// testOwner owns the inboxes: a Sandbox runs as the ServiceAccount of its own name.
var testOwner = action.ServiceAccount{Namespace: testNamespace, Name: testSandbox}

// testIncarnation is testOwner as the Sandbox the destinations name.
var testIncarnation = store.Owner{Account: testOwner, SandboxUID: testSandboxUID}

// wireEvent is the payload the Notification Service retains for an Action event.
func wireEvent(sequence int64) string {
	return fmt.Sprintf(`{"sequence":%d,"state":"decision_pending","at":"2026-10-20T03:56:03.123456Z","actor":null}`, sequence)
}

type recordedCall struct {
	Method        string
	RequestID     string
	AfterSequence int32
	Limit         int32
	Authorization string
}

type fakeActionService struct {
	actionpb.UnimplementedActionServiceServer
	mu               sync.Mutex
	request          *actionpb.ActionRequest
	eventCount       int32
	events           []*actionpb.ActionEvent
	useEventOverride bool
	getError         error
	eventsError      error
	blockGet         bool
	calls            []recordedCall
}

func (s *fakeActionService) GetActionRequest(ctx context.Context, request *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	s.mu.Lock()
	s.calls = append(s.calls, recordedCall{
		Method: "GetActionRequest", RequestID: request.GetRequestId(), Authorization: authorization(ctx),
	})
	response, err, block := s.request, s.getError, s.blockGet
	s.mu.Unlock()
	if block {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if err != nil {
		return nil, err
	}
	return &actionpb.GetActionRequestResponse{ActionRequest: response}, nil
}

func (s *fakeActionService) ListActionRequestEvents(ctx context.Context, request *actionpb.ListActionRequestEventsRequest) (*actionpb.ListActionRequestEventsResponse, error) {
	s.mu.Lock()
	s.calls = append(s.calls, recordedCall{
		Method: "ListActionRequestEvents", RequestID: request.GetRequestId(),
		AfterSequence: request.GetAfterSequence(), Limit: request.GetLimit(), Authorization: authorization(ctx),
	})
	eventCount, events, override, err := s.eventCount, s.events, s.useEventOverride, s.eventsError
	s.mu.Unlock()
	if err != nil {
		return nil, err
	}
	if override {
		return &actionpb.ListActionRequestEventsResponse{Events: events}, nil
	}
	page := make([]*actionpb.ActionEvent, 0, request.GetLimit())
	for sequence := request.GetAfterSequence() + 1; sequence <= eventCount && int32(len(page)) < request.GetLimit(); sequence++ {
		page = append(page, wireActionEvent(sequence))
	}
	return &actionpb.ListActionRequestEventsResponse{Events: page}, nil
}

func authorization(ctx context.Context) string {
	values := metadata.ValueFromIncomingContext(ctx, "authorization")
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func (s *fakeActionService) set(f func(*fakeActionService)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f(s)
}

func (s *fakeActionService) log() []recordedCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]recordedCall(nil), s.calls...)
}

func wireActionEvent(sequence int32) *actionpb.ActionEvent {
	return &actionpb.ActionEvent{
		Sequence: sequence,
		State:    actionpb.ActionState_ACTION_STATE_DECISION_PENDING,
		At:       timestamppb.New(time.Date(2026, 10, 20, 3, 56, 3, 123456000, time.UTC)),
	}
}

type fixture struct {
	db         *sql.DB
	store      *store.Store
	service    *fakeActionService
	server     *grpc.Server
	connection *grpc.ClientConn
	tokenFile  string
	listener   net.Listener
	source     *Source
}

// newFixture starts from a database without the other tests' inboxes: Claim takes whichever inbox is due.
func newFixture(t *testing.T) fixture {
	t.Helper()
	db, err := sql.Open("postgres", database.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	service := &fakeActionService{
		request: &actionpb.ActionRequest{Caller: &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: testSandbox}},
	}
	server := grpc.NewServer()
	actionpb.RegisterActionServiceServer(server, service)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	f := fixture{
		db: db, service: service, server: server, listener: listener, tokenFile: tokenFile,
		store: store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}),
	}
	f.source = &Source{Service: f.client(t, 5), Store: f.store}
	return f
}

func (f fixture) client(t *testing.T, timeoutSeconds float64) actionpb.ActionServiceClient {
	t.Helper()
	connection, err := bearer.Dial(f.listener.Addr().String(), f.tokenFile, time.Duration(timeoutSeconds*float64(time.Second)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return actionpb.NewActionServiceClient(connection)
}

func (f fixture) subscribe(t *testing.T) store.Subscription {
	t.Helper()
	subscription, err := f.store.Subscribe(t.Context(), testOwner, store.Subscribe{
		DestinationRef: store.DestinationRef{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID},
		SessionID:      "test-session",
		IdempotencyKey: "test-key",
		Source:         store.ActionsSource{RequestID: uuid.New()},
		LifetimeDays:   7,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) claim(t *testing.T) store.Claim {
	t.Helper()
	claim, err := f.store.Claim(t.Context())
	if err != nil || claim == nil {
		t.Fatalf("claim = %v, %v", claim, err)
	}
	return *claim
}

// reconcile reads the subscription's source as the worker does when it is due.
func (f fixture) reconcile(t *testing.T, claim store.Claim) error {
	t.Helper()
	return f.reconcileWith(t, f.source, claim)
}

func (f fixture) reconcileWith(t *testing.T, source *Source, claim store.Claim) error {
	t.Helper()
	if _, err := f.db.ExecContext(t.Context(), `UPDATE subscription SET next_attempt = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	due, err := f.store.Source(t.Context(), claim)
	if err != nil || due == nil {
		t.Fatalf("source = %v, %v", due, err)
	}
	return source.Reconcile(t.Context(), claim, *due)
}

func (f fixture) entries(t *testing.T, inboxID uuid.UUID) []store.Entry {
	t.Helper()
	page, err := f.store.Read(t.Context(), testIncarnation, inboxID, 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	return page.Entries
}

// lastCursor is the cursor of the inbox's newest entry, which is how many entries it holds.
func (f fixture) lastCursor(t *testing.T, inboxID uuid.UUID) int64 {
	t.Helper()
	page, err := f.store.Read(t.Context(), testIncarnation, inboxID, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	return page.Inbox.LastCursor
}

func requireFailure(t *testing.T, err error, kind store.SourceFailureKind, message string) {
	t.Helper()
	var failure *worker.ProviderError
	if !errors.As(err, &failure) {
		t.Fatalf("error = %v, want a *worker.ProviderError", err)
	}
	if failure.Kind != kind || failure.Message != message || failure.RetryAfter != 0 {
		t.Fatalf("failure = %+v, want kind %s, message %q and the default retry", *failure, kind, message)
	}
}

func TestReconcileRecordsTheEventsOfTheCallersRequest(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	requestID := subscription.Source.(store.ActionsSource).RequestID
	f.service.set(func(s *fakeActionService) { s.eventCount = 3 })
	claim := f.claim(t)

	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}

	entries := f.entries(t, subscription.InboxID)
	if len(entries) != 3 {
		t.Fatalf("entries = %+v, want 3", entries)
	}
	for i, entry := range entries {
		sequence := int64(i + 1)
		if !reflect.DeepEqual(entry.Event, store.ActionsEvent{RequestID: requestID, Sequence: sequence}) {
			t.Errorf("entry %d is %+v", sequence, entry.Event)
		}
		if got, want := string(entry.Payload), wireEvent(sequence); !sameJSON(t, got, want) {
			t.Errorf("payload %s, want %s", got, want)
		}
	}
	calls := f.service.log()
	if len(calls) != 2 || calls[0].Method != "GetActionRequest" || calls[1].Method != "ListActionRequestEvents" {
		t.Fatalf("calls = %+v, want request and event reads", calls)
	}
	for _, call := range calls {
		if call.RequestID != requestID.String() || call.Authorization != "Bearer test-token" {
			t.Errorf("call = %+v", call)
		}
	}
	if calls[1].AfterSequence != 0 || calls[1].Limit != eventsPageSize {
		t.Errorf("events request = %+v", calls[1])
	}

	// The token is read again for every RPC, and the next read resumes after the last sequence recorded.
	if err := os.WriteFile(f.tokenFile, []byte("rotated"), 0o600); err != nil {
		t.Fatal(err)
	}
	f.service.set(func(s *fakeActionService) { s.eventCount = 5 })
	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	calls = f.service.log()[2:]
	if len(calls) != 2 || calls[1].AfterSequence != 3 {
		t.Errorf("second read = %+v", calls)
	}
	for _, call := range calls {
		if call.Authorization != "Bearer rotated" {
			t.Errorf("Authorization after rotation = %q", call.Authorization)
		}
	}
	if got := len(f.entries(t, subscription.InboxID)); got != 5 {
		t.Errorf("entries = %d, want 5", got)
	}
}

func TestReconcileReadsALongLogAPageAtATime(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	f.service.set(func(s *fakeActionService) { s.eventCount = 130 })
	claim := f.claim(t)

	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	if got := f.lastCursor(t, subscription.InboxID); got != 128 {
		t.Fatalf("entries after the first read = %d, want 128", got)
	}
	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	if got := f.lastCursor(t, subscription.InboxID); got != 130 {
		t.Errorf("entries after the second read = %d, want 130", got)
	}
}

func TestReconcileDoesNotFollowARequestOfAnotherAccount(t *testing.T) {
	for name, caller := range map[string]*sandboxespb.ServiceAccount{
		"another account":                    {Namespace: testNamespace, Name: "another-sandbox"},
		"the same name in another namespace": {Namespace: "another-namespace", Name: testSandbox},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			subscription := f.subscribe(t)
			f.service.set(func(s *fakeActionService) { s.request.Caller = caller; s.eventCount = 3 })

			err := f.reconcile(t, f.claim(t))

			requireFailure(t, err, store.FailureAccessDenied, "SourceNotOwnedError")
			for _, call := range f.service.log() {
				if call.Method == "ListActionRequestEvents" {
					t.Errorf("events were requested: %+v", call)
				}
			}
			if got := len(f.entries(t, subscription.InboxID)); got != 0 {
				t.Errorf("entries = %d", got)
			}
		})
	}
}

func TestEventsReturnsTheOwnersEventsAndRefusesAnotherAccount(t *testing.T) {
	f := newFixture(t)
	f.service.set(func(s *fakeActionService) {
		s.useEventOverride = true
		s.events = []*actionpb.ActionEvent{wireActionEvent(2)}
	})
	request := uuid.New()

	events, err := f.source.Events(t.Context(), testOwner, request, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].Sequence != 2 || events[0].State != actionpb.ActionState_ACTION_STATE_DECISION_PENDING {
		t.Errorf("events = %+v, want the second", events)
	}
	call := f.service.log()[1]
	if call.AfterSequence != 1 || call.Limit != eventsPageSize || call.RequestID != request.String() {
		t.Errorf("events call = %+v", call)
	}

	_, err = f.source.Events(t.Context(), action.ServiceAccount{Namespace: testNamespace, Name: "another-sandbox"}, request, 0)
	requireFailure(t, err, store.FailureAccessDenied, "SourceNotOwnedError")
}

func TestEventsMapsTheActionEventActors(t *testing.T) {
	f := newFixture(t)
	f.service.set(func(s *fakeActionService) {
		s.useEventOverride = true
		s.events = []*actionpb.ActionEvent{
			{
				Sequence: 1, State: actionpb.ActionState_ACTION_STATE_ALLOWED,
				At: timestamppb.New(time.Date(2026, 10, 20, 3, 56, 3, 123456000, time.UTC)),
				Actor: &actionpb.Principal{Principal: &actionpb.Principal_Caller{Caller: &sandboxespb.ServiceAccount{
					Namespace: testNamespace, Name: testSandbox,
				}}},
			},
			{
				Sequence: 2, State: actionpb.ActionState_ACTION_STATE_DENIED,
				At: timestamppb.New(time.Date(2026, 10, 20, 3, 56, 4, 0, time.UTC)),
				Actor: &actionpb.Principal{Principal: &actionpb.Principal_Operator{Operator: &actionpb.Operator{
					Issuer: "test-issuer", Subject: "test-subject",
				}}},
			},
		}
	})

	events, err := f.source.Events(t.Context(), testOwner, uuid.New(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 || events[0].State != actionpb.ActionState_ACTION_STATE_ALLOWED || events[1].State != actionpb.ActionState_ACTION_STATE_DENIED {
		t.Fatalf("events = %+v", events)
	}
	if actor := events[0].Actor.GetCaller(); actor.GetNamespace() != testOwner.Namespace || actor.GetName() != testOwner.Name {
		t.Errorf("caller actor = %#v", events[0].Actor)
	}
	if actor := events[1].Actor.GetOperator(); actor.GetIssuer() != "test-issuer" || actor.GetSubject() != "test-subject" {
		t.Errorf("operator actor = %#v", events[1].Actor)
	}
}

func TestReconcileClassifiesActionServiceResponses(t *testing.T) {
	for name, tc := range map[string]struct {
		read    string
		setup   func(*fakeActionService)
		kind    store.SourceFailureKind
		message string
	}{
		"request not found": {
			read: "request", setup: func(s *fakeActionService) { s.getError = status.Error(codes.NotFound, "test detail") },
			kind: store.FailureAccessDenied, message: "NotFound",
		},
		"request unauthenticated": {
			read: "request", setup: func(s *fakeActionService) { s.getError = status.Error(codes.Unauthenticated, "test detail") },
			kind: store.FailureAccessDenied, message: "Unauthenticated",
		},
		"request unavailable": {
			read: "request", setup: func(s *fakeActionService) {
				s.getError = status.Error(codes.Unavailable, "the bearer token file is empty")
			},
			kind: store.FailureUnavailable, message: "TransportError",
		},
		"request invalid": {
			read: "request", setup: func(s *fakeActionService) { s.getError = status.Error(codes.InvalidArgument, "test detail") },
			kind: store.FailureProcessingError, message: "InvalidArgument",
		},
		"events permission denied": {
			read: "events", setup: func(s *fakeActionService) { s.eventsError = status.Error(codes.PermissionDenied, "test detail") },
			kind: store.FailureAccessDenied, message: "PermissionDenied",
		},
		"events rate limited": {
			read: "events", setup: func(s *fakeActionService) { s.eventsError = status.Error(codes.ResourceExhausted, "test detail") },
			kind: store.FailureRateLimited, message: "ResourceExhausted",
		},
		"events server failure": {
			read: "events", setup: func(s *fakeActionService) { s.eventsError = status.Error(codes.Internal, "test detail") },
			kind: store.FailureUnavailable, message: "TransportError",
		},
		"request without receipt": {
			read: "request", setup: func(s *fakeActionService) { s.request = nil },
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"request without caller": {
			read: "request", setup: func(s *fakeActionService) { s.request.Caller = nil },
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"caller without a name": {
			read: "request", setup: func(s *fakeActionService) { s.request.Caller.Name = "" },
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"event without a sequence": {
			read: "events", setup: func(s *fakeActionService) {
				s.useEventOverride, s.events = true, []*actionpb.ActionEvent{{At: wireActionEvent(1).GetAt(), State: actionpb.ActionState_ACTION_STATE_ALLOWED}}
			},
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"event with an unknown state": {
			read: "events", setup: func(s *fakeActionService) {
				event := wireActionEvent(1)
				event.State = actionpb.ActionState(99)
				s.useEventOverride, s.events = true, []*actionpb.ActionEvent{event}
			},
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"event without a time": {
			read: "events", setup: func(s *fakeActionService) {
				event := wireActionEvent(1)
				event.At = nil
				s.useEventOverride, s.events = true, []*actionpb.ActionEvent{event}
			},
			kind: store.FailureProcessingError, message: "ValidationError",
		},
		"event with an invalid actor": {
			read: "events", setup: func(s *fakeActionService) {
				event := wireActionEvent(1)
				event.Actor = &actionpb.Principal{Principal: &actionpb.Principal_Operator{Operator: &actionpb.Operator{Issuer: ""}}}
				s.useEventOverride, s.events = true, []*actionpb.ActionEvent{event}
			},
			kind: store.FailureProcessingError, message: "ValidationError",
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			subscription := f.subscribe(t)
			f.service.set(tc.setup)
			err := f.reconcile(t, f.claim(t))

			requireFailure(t, err, tc.kind, tc.message)
			calls := f.service.log()
			for _, call := range calls {
				if tc.read == "request" && call.Method == "ListActionRequestEvents" {
					t.Error("events were read after the request failed validation")
				}
			}
			if got := len(f.entries(t, subscription.InboxID)); got != 0 {
				t.Errorf("entries = %d", got)
			}
		})
	}
}

func TestReconcileShowsAnUnreachableActionServiceAsUnavailable(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	f.server.Stop()

	requireFailure(t, f.reconcile(t, claim), store.FailureUnavailable, "TransportError")
}

func TestReconcileShowsAnActionServiceCallThatExceedsItsTimeoutAsUnavailable(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	f.service.set(func(s *fakeActionService) { s.blockGet = true })
	impatient := &Source{Service: f.client(t, 0.05), Store: f.store}

	requireFailure(t, f.reconcileWith(t, impatient, claim), store.FailureUnavailable, "TransportError")
}

// A cancelled read is the worker stopping, not a source to show as failing.
func TestReconcileReturnsTheCancellationOfItsContext(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	if _, err := f.db.ExecContext(t.Context(), `UPDATE subscription SET next_attempt = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	due, err := f.store.Source(t.Context(), claim)
	if err != nil || due == nil {
		t.Fatalf("source = %v, %v", due, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	err = f.source.Reconcile(ctx, claim, *due)

	var failure *worker.ProviderError
	if !errors.Is(err, context.Canceled) || errors.As(err, &failure) {
		t.Errorf("error = %v, want the context's cancellation", err)
	}
}

func TestReconcileRefusesAGapInTheLogAndRecordsNothing(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	f.service.set(func(s *fakeActionService) {
		s.useEventOverride = true
		s.events = []*actionpb.ActionEvent{wireActionEvent(2), wireActionEvent(3)}
	})

	err := f.reconcile(t, f.claim(t))

	requireFailure(t, err, store.FailureProcessingError, "ConflictError")
	if got := len(f.entries(t, subscription.InboxID)); got != 0 {
		t.Errorf("entries = %d", got)
	}
}

// Python stops the worker when its projected token cannot be read; the failure is not shown and retried.
func TestAnUnreadableTokenIsNotAFailureOfTheSubscription(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	if err := os.Remove(f.tokenFile); err != nil {
		t.Fatal(err)
	}

	err := f.reconcile(t, claim)

	var failure *worker.ProviderError
	if err == nil || errors.As(err, &failure) || status.Code(err) != codes.Unavailable {
		t.Errorf("error = %v, want an unclassified token-file Unavailable error", err)
	}
	if got := f.service.log(); len(got) != 0 {
		t.Errorf("calls were made without a token: %+v", got)
	}
}

func sameJSON(t *testing.T, got, want string) bool {
	t.Helper()
	var a, b any
	if err := json.Unmarshal([]byte(got), &a); err != nil {
		t.Fatalf("%s: %v", got, err)
	}
	if err := json.Unmarshal([]byte(want), &b); err != nil {
		t.Fatalf("%s: %v", want, err)
	}
	return reflect.DeepEqual(a, b)
}
