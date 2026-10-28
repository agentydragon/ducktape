package api

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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
		Reader:  &reads.Reader{DB: db, History: history, NativeFrameReadBatchSize: 200},
		Wakeups: hub, Resolver: resolver,
		Callers:        []auth.Account{{Namespace: testNamespace, Name: "test-reader"}},
		RequestTimeout: requestTimeout, WatchLease: lease,
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
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
	if _, err := s.history.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	var position uint64
	if err := s.db.QueryRowContext(t.Context(), "SELECT opened_position FROM session_history WHERE id = $1", sessionID).Scan(&position); err != nil {
		t.Fatal(err)
	}
	return sessionID, position
}

func testEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-runner-journal", Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal"}}},
	}
}

func unary[Request, Response any](call func(context.Context, *Request, ...grpc.CallOption) (*Response, error)) func(context.Context) error {
	return func(ctx context.Context) error {
		_, err := call(ctx, new(Request))
		return err
	}
}

func TestEveryRPCAdmitsOnlyTheConfiguredCallers(t *testing.T) {
	server := newServer(t, time.Minute)
	calls := map[string]func(context.Context) error{
		"ReadSessionEvents":        unary(server.client.ReadSessionEvents),
		"ReadSessionObservations":  unary(server.client.ReadSessionObservations),
		"GetSessionSummary":        unary(server.client.GetSessionSummary),
		"BatchGetSessionSummaries": unary(server.client.BatchGetSessionSummaries),
		"FindSessions":             unary(server.client.FindSessions),
		"ReadThreadEntities":       unary(server.client.ReadThreadEntities),
		"ReadEntityEvidence":       unary(server.client.ReadEntityEvidence),
		"ReadNativeFrames":         unary(server.client.ReadNativeFrames),
		"FollowSession": func(ctx context.Context) error {
			sessionID, _ := server.record(t)
			stream, err := server.client.FollowSession(ctx, &historypb.FollowSessionRequest{
				SessionId: sessionID.String(), Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: 0},
			})
			if err != nil {
				return err
			}
			_, err = stream.Recv()
			return err
		},
		"WatchSessionSummaries": func(ctx context.Context) error {
			stream, err := server.client.WatchSessionSummaries(ctx, &historypb.WatchSessionSummariesRequest{})
			if err != nil {
				return err
			}
			_, err = stream.Recv()
			return err
		},
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			for token, want := range map[string]codes.Code{"": codes.Unauthenticated, "test-token-unknown": codes.Unauthenticated, strangerToken: codes.PermissionDenied} {
				if got := status.Code(call(bearing(t, token))); got != want {
					t.Errorf("with token %q: %v, want %v", token, got, want)
				}
			}
			if got := status.Code(call(bearing(t, readerToken))); got == codes.Unauthenticated || got == codes.PermissionDenied {
				t.Errorf("an allowed caller was refused: %v", got)
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
		"admission refusal":        {t.Context(), status.Error(codes.PermissionDenied, "refused"), codes.PermissionDenied},
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
