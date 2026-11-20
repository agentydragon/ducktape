package rpc

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"maps"
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	testAudience        = "test-history-service"
	testNamespace       = "test-reader-namespace"
	testPolicyNamespace = "test-history-policies"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
)

var (
	kube        *kubeapiserver.Server
	readerToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	ctx := context.Background()
	if readerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: testNamespace, Name: "test-reader"}, testAudience); err != nil {
		return err
	}
	return nil
}

var testWriter = storage.ServiceAccountWriter{Namespace: testNamespace, Name: "test-writer", UID: "test-writer-uid"}

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

// newSVIDResolver admits the workload callers of the test API server.
func newSVIDResolver(t *testing.T) *auth.SVIDResolver {
	t.Helper()
	svids, err := auth.NewKubernetesSVIDResolver(kube.Config, []string{testNamespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return svids
}

// newServerTimingOut serves the API to the app as a direct service reader.
func newServerTimingOut(t *testing.T, lease, requestTimeout time.Duration) testServer {
	t.Helper()
	server, connect := newServiceAccessServer(t, lease, requestTimeout, []string{peertls.AppURI}, nil, nil)
	server.client = connect(t, peertls.AppURI)
	return server
}

// newServiceAccessServer serves the API over the shared database with a hub listening to the channels the watch
// follows. The app may connect whether or not it is among directCallers.
func newServiceAccessServer(
	t *testing.T,
	lease, requestTimeout time.Duration,
	directCallers []string,
	serviceDelegators []auth.ServiceDelegator,
	identities auth.IdentityUIDReader,
) (testServer, func(*testing.T, string) historypb.HistoryServiceClient) {
	t.Helper()
	db := postgres.Open(t, integrationDatabase.URL)
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, storage.ChannelHistory, projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	history := storage.NewStore(db, storage.Options{SettleDeltas: true})
	service := NewService(Options{
		Reader:  &reads.Reader{DB: db, History: history, NativeFrameReadBatchSize: 200},
		Wakeups: hub, RequestTimeout: requestTimeout, WatchLease: lease,
	})
	methods, err := Methods(newSVIDResolver(t)).WithServiceCallers(ReadMethods(), directCallers)
	if err != nil {
		t.Fatal(err)
	}
	if len(serviceDelegators) > 0 {
		if methods, err = methods.WithServiceDelegation(ReadMethods(), serviceDelegators, identities); err != nil {
			t.Fatal(err)
		}
	}
	callerURIs := append([]string{peertls.AppURI}, directCallers...)
	for _, delegator := range serviceDelegators {
		callerURIs = append(callerURIs, delegator.SPIFFEID)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, requestTimeout, peertls.HistoryIngressURI, methods, func(server *grpc.Server) {
		historypb.RegisterHistoryServiceServer(server, service)
	}, callerURIs...)
	connect := func(t *testing.T, caller string) historypb.HistoryServiceClient {
		t.Helper()
		return historypb.NewHistoryServiceClient(grpcmethods.AsCaller(connection, caller))
	}
	return testServer{service: service, hub: hub, history: history, db: db}, connect
}

// callContext supplies the test's normal call deadline; all admitted calls use the client SVID.
func callContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return ctx
}

// record has the Session recorded as a journal writer's first push does, and returns its position.
func (s testServer) record(t *testing.T) (uuid.UUID, uint64) {
	t.Helper()
	sessionID := uuid.New()
	if _, err := s.history.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	var position uint64
	if err := s.db.QueryRowContext(t.Context(), "SELECT recorded_position FROM session_history WHERE id = $1", sessionID).Scan(&position); err != nil {
		t.Fatal(err)
	}
	return sessionID, position
}

func (s testServer) recordAs(t *testing.T, identity storage.Identity) uuid.UUID {
	t.Helper()
	sessionID := uuid.New()
	if _, err := s.history.BindSamplerWriter(t.Context(), sessionID, testWriter,
		storage.Sandbox{Namespace: testNamespace, Name: testWriter.Name, UID: uuid.NewString()}, &identity); err != nil {
		t.Fatal(err)
	}
	return sessionID
}

// Legacy account tuples are retained audit data, never new ownership assignments.
func serviceDelegatedIdentity(t *testing.T, identity storage.Identity) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: uuid.NewString()}},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
}

func testEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-runner-journal", Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal"}}},
	}
}

func TestEveryRPCRequiresAWorkloadSVID(t *testing.T) {
	methods := Methods(newSVIDResolver(t))
	register := func(server *grpc.Server) {
		historypb.RegisterHistoryServiceServer(server, historypb.UnimplementedHistoryServiceServer{})
	}
	connection := grpcmethods.Serve(t, methods, register)
	for _, method := range grpcmethods.Of(historypb.HistoryService_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			for name, ctx := range map[string]context.Context{
				"no credential": t.Context(),
				"bearer only":   metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+readerToken),
			} {
				if got := status.Code(method.Call(ctx, connection)); got != codes.Unauthenticated {
					t.Errorf("%s: %v, want Unauthenticated", name, got)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(t.Context(), grpcmethods.Serve(t, unlisted, register))); got != codes.PermissionDenied {
				t.Errorf("unlisted method got %v", got)
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
	ctx := callContext(t)

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

func TestServiceReaderAndDelegatedServiceUseSeparateReadScopes(t *testing.T) {
	owner := storage.Identity{Name: "service-scope-owner-" + uuid.NewString(), UID: uuid.NewString()}
	other := storage.Identity{Name: "service-scope-other-" + uuid.NewString(), UID: uuid.NewString()}
	server, connect := newServiceAccessServer(
		t,
		time.Second,
		callDeadline,
		[]string{peertls.NotificationsURI},
		[]auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}},
		newLiveIdentities(owner, other),
	)
	owned := server.recordAs(t, owner)
	foreign := server.recordAs(t, other)
	legacy, _ := server.record(t)
	for _, sessionID := range []uuid.UUID{owned, foreign, legacy} {
		if _, err := server.history.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{testEntry(1)}); err != nil {
			t.Fatal(err)
		}
	}
	request := func(sessionID uuid.UUID) *historypb.ReadSessionEventsRequest {
		return &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), Limit: 10}
	}

	// A configured direct service reader has the backend-wide scope, including retained
	// Identity-owned and legacy ServiceAccount-owned Sessions.
	directReader := connect(t, peertls.NotificationsURI)
	for _, sessionID := range []uuid.UUID{owned, foreign, legacy} {
		page, err := directReader.ReadSessionEvents(callContext(t), request(sessionID))
		if err != nil || len(page.GetEntries()) != 1 {
			t.Errorf("allowlisted direct reader of %s = %+v, %v", sessionID, page, err)
		}
	}

	// The delegator has no direct reader grant, and an unrelated service has neither grant.
	for name, actor := range map[string]string{
		"delegator without envelope": peertls.ActionsURI,
		"unlisted service":           peertls.AppURI,
	} {
		t.Run(name, func(t *testing.T) {
			client := connect(t, actor)
			if _, err := client.ReadSessionEvents(callContext(t), request(owned)); status.Code(err) != codes.PermissionDenied {
				t.Fatalf("direct service read = %v, want PermissionDenied", err)
			}
		})
	}

	delegator := connect(t, peertls.ActionsURI)
	delegated := serviceDelegatedIdentity(t, owner)
	page, err := delegator.ReadSessionEvents(delegated, request(owned))
	if err != nil || len(page.GetEntries()) != 1 {
		t.Fatalf("delegated owner read = %+v, %v", page, err)
	}
	for _, sessionID := range []uuid.UUID{foreign, legacy} {
		if _, err := delegator.ReadSessionEvents(delegated, request(sessionID)); status.Code(err) != codes.NotFound {
			t.Errorf("delegated read of other Identity or legacy Session %s = %v, want NotFound", sessionID, err)
		}
	}
}

func TestAnUnfoldedOrUnknownSessionReadsAsNotFoundWhereItHasNoHistory(t *testing.T) {
	server := newServer(t, time.Minute)
	ctx := callContext(t)
	sessionID := uuid.NewString()
	entity := &historypb.EntityKey{EntityKind: historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM, EntityId: "test-entity"}
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
	_, err := server.client.GetSessionSummary(callContext(t), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
	if got := status.Code(err); got != codes.DeadlineExceeded {
		t.Errorf("%v, want DeadlineExceeded", got)
	}
}

type liveIdentities struct {
	uids map[string]string
}

func newLiveIdentities(identities ...storage.Identity) *liveIdentities {
	live := &liveIdentities{uids: make(map[string]string)}
	for _, identity := range identities {
		live.uids[identity.Name] = identity.UID
	}
	return live
}

func (live *liveIdentities) GetIdentityUID(_ context.Context, name string) (string, bool, error) {
	uid, found := live.uids[name]
	return uid, found, nil
}
