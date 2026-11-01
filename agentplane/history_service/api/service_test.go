package api

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"maps"
	"net"
	"sort"
	"strings"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience  = "test-history-service"
	testNamespace = "test-reader-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
)

var (
	kube *kubeapiserver.Server
	// readerToken proves test-reader, an allowed caller; strangerToken proves test-stranger, which is
	// not.
	readerToken, strangerToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	ctx := context.Background()
	if readerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: testNamespace, Name: "test-reader"}, testAudience); err != nil {
		return err
	}
	strangerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: testNamespace, Name: "test-stranger"}, testAudience)
	return err
}

var testWriter = storage.Writer{Namespace: testNamespace, Name: "test-writer", UID: "test-writer-uid"}

type testServer struct {
	service *Service
	hub     *wakeups.Hub
	client  historypb.HistoryServiceClient
	history *storage.Store
	db      *sql.DB
}

func newServer(t *testing.T, lease time.Duration) testServer {
	t.Helper()
	return newServerTimingOut(t, lease, callDeadline)
}

// newServerTimingOut serves the API over the shared database with a hub listening to the channels the watch
// follows.
func newServerTimingOut(t *testing.T, lease, requestTimeout time.Duration) testServer {
	return newServerWithDelegation(t, lease, requestTimeout, nil, nil)
}

func newDelegatingServer(t *testing.T, lease time.Duration) testServer {
	actor := auth.Account{Namespace: testNamespace, Name: "test-reader"}
	uidReader := auth.ServiceAccountUIDReaderFunc(func(ctx context.Context, account auth.Account) (string, bool, error) {
		serviceAccount, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Get(ctx, account.Name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			return "", false, nil
		}
		if err != nil {
			return "", false, err
		}
		return string(serviceAccount.UID), true, nil
	})
	return newServerWithDelegation(t, lease, callDeadline,
		&auth.Delegator{Account: actor, CallerNamespaces: []string{testNamespace}}, uidReader)
}

func newServerWithDelegation(
	t *testing.T,
	lease, requestTimeout time.Duration,
	delegator *auth.Delegator,
	uidReader auth.ServiceAccountUIDReader,
) testServer {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, storage.ChannelHistory, projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	history := storage.NewStore(db, true)
	service := NewService(Options{
		Reader:         &reads.Reader{DB: db, History: history, NativeFrameReadBatchSize: 200},
		ReaderAccounts: []auth.Account{{Namespace: testNamespace, Name: "test-reader"}},
		Wakeups:        hub, RequestTimeout: requestTimeout, WatchLease: lease,
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver)
	if delegator != nil {
		delegatedMethods := make([]string, 0, len(methods))
		for method := range methods {
			delegatedMethods = append(delegatedMethods, method)
		}
		sort.Strings(delegatedMethods)
		methods, err = MethodsWithDelegation(resolver, *delegator, uidReader, delegatedMethods)
		if err != nil {
			t.Fatal(err)
		}
	}
	server := grpc.NewServer(methods.ServerOptions(requestTimeout)...)
	historypb.RegisterHistoryServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return testServer{service: service, hub: hub, client: historypb.NewHistoryServiceClient(connection), history: history, db: db}
}

// bearing is a call context carrying the token, or none for "".
func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	if token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// record has the Session recorded as a runner's first push does, and returns its position.
func (s testServer) record(t *testing.T) (uuid.UUID, uint64) {
	t.Helper()
	sessionID := uuid.New()
	if _, err := s.history.BindWriter(t.Context(), sessionID, testWriter, "", nil); err != nil {
		t.Fatal(err)
	}
	var position uint64
	if err := s.db.QueryRowContext(t.Context(), "SELECT opened_position FROM session_history WHERE id = $1", sessionID).Scan(&position); err != nil {
		t.Fatal(err)
	}
	return sessionID, position
}

func (s testServer) recordFor(t *testing.T, principal storage.ExecutionPrincipal) (uuid.UUID, uint64) {
	t.Helper()
	sessionID := uuid.New()
	if _, err := s.history.BindWriter(t.Context(), sessionID, testWriter, "", &principal); err != nil {
		t.Fatal(err)
	}
	var position uint64
	if err := s.db.QueryRowContext(t.Context(), "SELECT opened_position FROM session_history WHERE id = $1", sessionID).Scan(&position); err != nil {
		t.Fatal(err)
	}
	return sessionID, position
}

func delegated(t *testing.T, token string, caller auth.Account, uid string) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, Uid: uid,
		},
		ActionRequestId: uuid.NewString(), ActionExecutionId: uuid.NewString(),
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token, auth.DelegationMetadataKey, string(encoded),
	))
}

func testEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-runner-journal", Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal"}}},
	}
}

func TestEveryRPCRequiresAReviewedServiceAccount(t *testing.T) {
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver)
	register := func(server *grpc.Server) {
		historypb.RegisterHistoryServiceServer(server, historypb.UnimplementedHistoryServiceServer{})
	}
	connection := grpcmethods.Serve(t, methods, register)
	for _, method := range grpcmethods.Of(historypb.HistoryService_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			// The stand-in servicer answers Unimplemented to any reviewed account in the served namespace.
			for token, want := range map[string]codes.Code{
				"": codes.Unauthenticated, "test-token-unknown": codes.Unauthenticated,
				strangerToken: codes.Unimplemented, readerToken: codes.Unimplemented,
			} {
				if got := status.Code(method.Call(bearing(t, token), connection)); got != want {
					t.Errorf("with token %q: %v, want %v", token, got, want)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(t, readerToken), grpcmethods.Serve(t, unlisted, register))); got != codes.PermissionDenied {
				t.Errorf("unlisted, an allowed caller got %v", got)
			}
		})
	}
}

func TestAFailureReachesTheClientAsAFixedStatus(t *testing.T) {
	expired, cancelExpired := context.WithDeadline(t.Context(), time.Now().Add(-time.Second))
	defer cancelExpired()
	canceled, cancel := context.WithCancel(t.Context())
	cancel()
	for name, test := range map[string]struct {
		ctx  context.Context
		err  error
		want codes.Code
	}{
		"unknown Session":          {t.Context(), fmt.Errorf("%w: a Session", storage.ErrNotFound), codes.NotFound},
		"unknown evidence":         {t.Context(), fmt.Errorf("%w: a Session", reads.ErrEvidenceNotFound), codes.NotFound},
		"reset epoch":              {t.Context(), fmt.Errorf("%w: a Session", reads.ErrScopeReset), codes.FailedPrecondition},
		"invalid stored entity":    {t.Context(), fmt.Errorf("%w: a row", reads.ErrInvalidStoredEntity), codes.Internal},
		"invalid request":          {t.Context(), fmt.Errorf("%w: a limit", storage.ErrInvalidArgument), codes.InvalidArgument},
		"answered status":          {t.Context(), status.Error(codes.PermissionDenied, "refused"), codes.PermissionDenied},
		"expired request deadline": {expired, errors.New("query canceled"), codes.DeadlineExceeded},
		"canceled call":            {canceled, errors.New("query canceled"), codes.Canceled},
		"backend failure":          {t.Context(), errors.New("pq: password authentication failed for user secret-user"), codes.Unavailable},
	} {
		t.Run(name, func(t *testing.T) {
			got := status.Convert(failure(test.ctx, test.err))
			if got.Code() != test.want {
				t.Errorf("%v, want %v", got.Code(), test.want)
			}
			if strings.Contains(got.Message(), "secret-user") {
				t.Errorf("the client was told the cause: %q", got.Message())
			}
		})
	}
}

func TestSessionHistoryIsReadOverGRPC(t *testing.T) {
	server := newServer(t, time.Minute)
	sessionID, _ := server.record(t)
	if _, err := server.history.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{testEntry(1), testEntry(2), testEntry(3)}); err != nil {
		t.Fatal(err)
	}
	ctx := bearing(t, readerToken)

	events, err := server.client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), AfterCursor: 1, Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(events.GetEntries()) != 1 || events.GetEntries()[0].GetCursor() != 2 || events.GetLastCursor() != 3 {
		t.Errorf("events after 1 (limit 1): %v", events)
	}
	if _, err := server.db.ExecContext(t.Context(),
		"INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor) VALUES ($1, 2, 3)", sessionID); err != nil {
		t.Fatal(err)
	}
	observations, err := server.client.ReadSessionObservations(ctx, &historypb.ReadSessionObservationsRequest{SessionId: sessionID.String(), Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if settled := observations.GetSettled(); len(settled) != 1 || settled[0].GetFirst() != 2 || settled[0].GetLast() != 3 {
		t.Errorf("settled %v, want the range 2 through 3", settled)
	}
	var cursors []uint64
	for _, observation := range observations.GetObservations() {
		cursors = append(cursors, observation.GetCursor())
	}
	if len(cursors) != 3 || cursors[0] != 1 || cursors[2] != 3 {
		t.Errorf("observation cursors %v, want 1 through 3", cursors)
	}
	summary, err := server.client.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
	if err != nil {
		t.Fatal(err)
	}
	if summary.GetCommittedCursor() != 3 {
		t.Errorf("summary committed through %d, want 3", summary.GetCommittedCursor())
	}
	unrecorded := uuid.NewString()
	batch, err := server.client.BatchGetSessionSummaries(ctx, &historypb.BatchGetSessionSummariesRequest{SessionIds: []string{sessionID.String(), unrecorded}})
	if err != nil {
		t.Fatal(err)
	}
	if len(batch.GetSummaries()) != 1 || batch.GetSummaries()[0].GetSessionId() != sessionID.String() {
		t.Errorf("batch %v, want only the recorded Session", batch)
	}
	found, err := server.client.FindSessions(ctx, &historypb.FindSessionsRequest{Sandbox: &historypb.FindSessionsRequest_SandboxName{SandboxName: testWriter.Name}})
	if err != nil {
		t.Fatal(err)
	}
	if !containsSession(found, sessionID) {
		t.Errorf("Sessions written by %q: %v, want %s among them", testWriter.Name, found, sessionID)
	}
}

func containsSession(found *historypb.FindSessionsResponse, sessionID uuid.UUID) bool {
	for _, session := range found.GetSessions() {
		if session.GetSessionId() == sessionID.String() {
			return true
		}
	}
	return false
}

func TestDirectAndDelegatedHistoryUseTheSameRetainedPrincipalScope(t *testing.T) {
	server := newDelegatingServer(t, time.Second)
	caller := auth.Account{Namespace: testNamespace, Name: "scope-caller-" + uuid.NewString()}
	foreign := auth.Account{Namespace: testNamespace, Name: "scope-foreign-" + uuid.NewString()}
	callerToken, err := kube.WorkloadToken(t.Context(), caller, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kube.WorkloadToken(t.Context(), foreign, testAudience); err != nil {
		t.Fatal(err)
	}
	callerSA, err := kube.Clientset.CoreV1().ServiceAccounts(caller.Namespace).Get(t.Context(), caller.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	foreignSA, err := kube.Clientset.CoreV1().ServiceAccounts(foreign.Namespace).Get(t.Context(), foreign.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	callerPrincipal := storage.ExecutionPrincipal{Namespace: caller.Namespace, Name: caller.Name, UID: string(callerSA.UID)}
	foreignPrincipal := storage.ExecutionPrincipal{Namespace: foreign.Namespace, Name: foreign.Name, UID: string(foreignSA.UID)}
	position := uint64(0)
	owned, _ := server.recordFor(t, callerPrincipal)
	foreignSession, _ := server.recordFor(t, foreignPrincipal)
	legacy, _ := server.record(t)
	for _, sessionID := range []uuid.UUID{owned, foreignSession, legacy} {
		if _, err := server.history.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{testEntry(1)}); err != nil {
			t.Fatal(err)
		}
	}
	direct := bearing(t, callerToken)
	action := delegated(t, readerToken, caller, callerPrincipal.UID)

	for name, ctx := range map[string]context.Context{"direct": direct, "delegated": action} {
		t.Run(name, func(t *testing.T) {
			for _, sessionID := range []uuid.UUID{owned} {
				response, err := server.client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), Limit: 10})
				if err != nil || len(response.GetEntries()) != 1 || response.GetEntries()[0].GetCursor() != 1 {
					t.Fatalf("owned raw read: %+v %v", response, err)
				}
				if _, err := server.client.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()}); err != nil {
					t.Fatalf("owned summary: %v", err)
				}
			}
			for _, sessionID := range []uuid.UUID{foreignSession, legacy} {
				if _, err := server.client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), Limit: 10}); status.Code(err) != codes.NotFound {
					t.Errorf("foreign or unassociated raw read = %v, want NotFound", err)
				}
			}
			batch, err := server.client.BatchGetSessionSummaries(ctx, &historypb.BatchGetSessionSummariesRequest{
				SessionIds: []string{owned.String(), foreignSession.String(), legacy.String()},
			})
			if err != nil || len(batch.GetSummaries()) != 1 || batch.GetSummaries()[0].GetSessionId() != owned.String() {
				t.Fatalf("scoped summary batch: %+v %v", batch, err)
			}
			found, err := server.client.FindSessions(ctx, &historypb.FindSessionsRequest{
				Sandbox: &historypb.FindSessionsRequest_SandboxName{SandboxName: testWriter.Name},
			})
			if err != nil || !containsSession(found, owned) || containsSession(found, foreignSession) || containsSession(found, legacy) {
				t.Fatalf("scoped Session search: %+v %v", found, err)
			}

			follow, err := server.client.FollowSession(ctx, &historypb.FollowSessionRequest{
				SessionId: owned.String(), Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: 0},
			})
			if err != nil {
				t.Fatalf("owned follow: %v", err)
			}
			var entries int
			for {
				frame, err := follow.Recv()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Fatalf("owned follow frame: %v", err)
				}
				if page := frame.GetEntries(); page != nil {
					entries += len(page.GetEntries())
				}
			}
			if entries != 1 {
				t.Fatalf("owned FollowSession returned %d entries", entries)
			}
			foreignFollow, err := server.client.FollowSession(ctx, &historypb.FollowSessionRequest{
				SessionId: foreignSession.String(), Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: 0},
			})
			if err == nil {
				_, err = foreignFollow.Recv()
			}
			if status.Code(err) != codes.NotFound {
				t.Errorf("foreign FollowSession = %v, want NotFound", err)
			}

			watchCtx, stopWatch := context.WithTimeout(ctx, 100*time.Millisecond)
			watch, err := server.client.WatchSessionSummaries(watchCtx, &historypb.WatchSessionSummariesRequest{OpenedAfter: proto.Uint64(position)})
			if err != nil {
				stopWatch()
				t.Fatalf("open scoped watch: %v", err)
			}
			changed, err := watch.Recv()
			if err != nil || changed.GetChanged() == nil {
				stopWatch()
				t.Fatalf("watch changed frame: %+v %v", changed, err)
			}
			opened, err := watch.Recv()
			if err != nil || opened.GetSessionOpened().GetSessionId() != owned.String() {
				stopWatch()
				t.Fatalf("watch leaked foreign or legacy opening: %+v %v", opened, err)
			}
			for {
				frame, err := watch.Recv()
				if err != nil {
					break
				}
				if opened := frame.GetSessionOpened(); opened != nil && opened.GetSessionId() != owned.String() {
					stopWatch()
					t.Fatalf("watch leaked another principal's opening: %v", opened)
				}
			}
			stopWatch()
		})
	}

	watchCtx, stopWatch := context.WithTimeout(delegated(t, readerToken, caller, callerPrincipal.UID), 5*time.Second)
	watch, err := server.client.WatchSessionSummaries(watchCtx, &historypb.WatchSessionSummariesRequest{OpenedAfter: proto.Uint64(position)})
	if err != nil {
		stopWatch()
		t.Fatal(err)
	}
	if changed, err := watch.Recv(); err != nil || changed.GetChanged() == nil {
		stopWatch()
		t.Fatalf("revalidation watch changed frame: %+v %v", changed, err)
	}
	if opened, err := watch.Recv(); err != nil || opened.GetSessionOpened().GetSessionId() != owned.String() {
		stopWatch()
		t.Fatalf("revalidation watch owner frame: %+v %v", opened, err)
	}
	if err := kube.RecreateServiceAccount(t.Context(), caller); err != nil {
		stopWatch()
		t.Fatal(err)
	}
	var reconnected bool
	for {
		frame, err := watch.Recv()
		if err != nil {
			stopWatch()
			t.Fatalf("watch did not complete its bounded lease: %v", err)
		}
		if frame.GetReconnectRequired() != nil {
			reconnected = true
			break
		}
	}
	stopWatch()
	if !reconnected {
		t.Fatal("delegated watch lease did not request fresh authorization")
	}
	if _, err := server.client.ReadSessionEvents(delegated(t, readerToken, caller, callerPrincipal.UID), &historypb.ReadSessionEventsRequest{SessionId: owned.String(), Limit: 10}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegation with replaced effective caller UID = %v, want PermissionDenied", err)
	}
}

func TestAnUnfoldedOrUnknownSessionReadsAsNotFoundWhereItHasNoHistory(t *testing.T) {
	server := newServer(t, time.Minute)
	ctx := bearing(t, readerToken)
	sessionID := uuid.NewString()
	entity := &historypb.EntityKey{EntityKind: "item", EntityId: "test-entity"}
	for name, call := range map[string]func() error{
		"events": func() error {
			_, err := server.client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: sessionID, Limit: 10})
			return err
		},
		"summary": func() error {
			_, err := server.client.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID})
			return err
		},
		"evidence": func() error {
			_, err := server.client.ReadEntityEvidence(ctx, &historypb.ReadEntityEvidenceRequest{SessionId: sessionID, ProjectionEpoch: "test-epoch", Entity: entity, Limit: 10})
			return err
		},
		"native frames": func() error {
			_, err := server.client.ReadNativeFrames(ctx, &historypb.ReadNativeFramesRequest{SessionId: sessionID, ProjectionEpoch: "test-epoch", Entity: entity, ObservationCursor: 1, Limit: 10})
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if got := status.Code(call()); got != codes.NotFound {
				t.Errorf("%v, want NotFound", got)
			}
		})
	}
	_, err := server.client.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: "not-a-uuid"})
	if got := status.Code(err); got != codes.InvalidArgument {
		t.Errorf("a malformed Session ID: %v, want InvalidArgument", got)
	}
}

func TestACallThatOutlastsTheRequestTimeoutIsAbandoned(t *testing.T) {
	server := newServerTimingOut(t, time.Hour, time.Nanosecond)
	sessionID, _ := server.record(t)
	_, err := server.client.GetSessionSummary(bearing(t, readerToken), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
	if got := status.Code(err); got != codes.DeadlineExceeded {
		t.Errorf("%v, want DeadlineExceeded", got)
	}
}
