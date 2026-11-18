package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
const callDeadline = 30 * time.Second

var (
	kube *kubeapiserver.Server
	// These valid legacy bearer tokens are used only for rejection checks.
	readerToken, writerToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	ctx := context.Background()
	audiences := []string{"test-history-service", "test-runner-ingress"}
	if readerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: "test-reader-namespace", Name: "test-reader"}, audiences...); err != nil {
		return err
	}
	writerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: "test-sandbox-namespace", Name: "test-sandbox"}, audiences...)
	if err != nil {
		return err
	}
	account, err := server.Clientset.CoreV1().ServiceAccounts("test-sandbox-namespace").Get(ctx, "test-sandbox", metav1.GetOptions{})
	if err != nil {
		return err
	}
	controller := true
	account.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
		Name: "test-sandbox", UID: "test-sandbox-uid", Controller: &controller,
	}}
	_, err = server.Clientset.CoreV1().ServiceAccounts("test-sandbox-namespace").Update(ctx, account, metav1.UpdateOptions{})
	return err
}

// running is a served History Service with reads and journal ingress on one listener.
type running struct {
	rpcAddress string
	reads      historypb.HistoryServiceClient
	health     healthpb.HealthClient
	ingress    session_ingresspb.SessionJournalIngressClient
	runner     *identitypb.Identity
	stop       func() error
}

// start serves with settings for the test database; tune adjusts the rest.
func start(t *testing.T, tune func(*config)) running {
	t.Helper()
	return startWithSVIDs(t, peertlstesting.NewCA(t), tune)
}

func callContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return ctx
}

func journalEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-runner-journal", Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal"}}},
	}
}

// pushJournal returns once the Session journal's last entry is acknowledged.
func pushJournal(t *testing.T, service running, sessionID uuid.UUID, entries ...*eventlogpb.EventEntry) {
	t.Helper()
	push, err := service.ingress.Push(callContext(t))
	if err != nil {
		t.Fatal(err)
	}
	frames := []*session_ingresspb.PushFrame{
		{Frame: &session_ingresspb.PushFrame_Announce{Announce: &sessionpb.Attached{SessionId: sessionID.String(), ExecutionIdentity: service.runner}}},
	}
	for _, entry := range entries {
		frames = append(frames, &session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_Entry{Entry: entry}})
	}
	for _, frame := range frames {
		if err := push.Send(frame); err != nil {
			t.Fatal(err)
		}
	}
	last := entries[len(entries)-1].GetCursor()
	for acknowledged := uint64(0); acknowledged < last; {
		acknowledgement, err := push.Recv()
		if err != nil {
			t.Fatalf("the push ended before acknowledging %d: %v", last, err)
		}
		acknowledged = acknowledgement.GetCursor()
	}
}

// A pushed journal is stored, folded, announced to a watcher and read back on the same native gRPC listener.
func TestAJournalPushedToTheIngressIsFoldedAndReadFromTheAPI(t *testing.T) {
	service := start(t, nil)
	watch, err := service.reads.WatchSessionSummaries(callContext(t), &historypb.WatchSessionSummariesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("the watch did not open: %v", err)
	}

	sessionID := uuid.New()
	pushJournal(t, service, sessionID, journalEntry(1), journalEntry(2), journalEntry(3))

	for {
		summary, err := service.reads.GetSessionSummary(callContext(t), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
		if err != nil {
			t.Fatal(err)
		}
		if summary.GetCommittedCursor() == 3 && summary.GetFoldedThrough() == 3 {
			return
		}
		// The fold announces its progress to watchers; a watch that ends fails the test.
		if _, err := watch.Recv(); err != nil {
			t.Fatalf("the watch ended before the fold caught up (committed %d, folded %d): %v",
				summary.GetCommittedCursor(), summary.GetFoldedThrough(), err)
		}
	}
}

func TestTheIngressSettlesDeltasOnlyWhenConfigured(t *testing.T) {
	for _, settleDeltas := range []bool{true, false} {
		t.Run(strconv.FormatBool(settleDeltas), func(t *testing.T) {
			service := start(t, func(c *config) { c.Ingress.SettleDeltas = settleDeltas })
			sessionID := uuid.New()
			journal := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}).Entries
			pushJournal(t, service, sessionID, journal...)
			observations, err := service.reads.ReadSessionObservations(callContext(t),
				&historypb.ReadSessionObservationsRequest{SessionId: sessionID.String(), Limit: uint32(len(journal))})
			if err != nil {
				t.Fatal(err)
			}
			// Cursors 3 through 6 are the two streamed chunk pairs the completion repeats.
			var got, want [][2]uint64
			for _, settled := range observations.GetSettled() {
				got = append(got, [2]uint64{settled.GetFirst(), settled.GetLast()})
			}
			if settleDeltas {
				want = [][2]uint64{{3, 6}}
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("settled %v, want %v", got, want)
			}
		})
	}
}

func TestIncrementalCompactionFlagPreservesPublicReadsAndNativeEvidence(t *testing.T) {
	for _, compact := range []bool{false, true} {
		t.Run(strconv.FormatBool(compact), func(t *testing.T) {
			service := start(t, func(c *config) {
				c.Ingress.CompactDeltas, c.Ingress.SettleDeltas = compact, false
			})
			watch, err := service.reads.WatchSessionSummaries(callContext(t), &historypb.WatchSessionSummariesRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := watch.Recv(); err != nil {
				t.Fatal(err)
			}
			chunks := make([]string, 96)
			for index := range chunks {
				chunks[index] = "hello "
			}
			span := spans.CodexReasoning([][]string{chunks})
			prefix := span.Entries[:len(span.Entries)-2]
			sessionID := uuid.New()
			pushJournal(t, service, sessionID, prefix...)
			page, err := service.reads.ReadSessionEvents(callContext(t), &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), Limit: 1000})
			if err != nil {
				t.Fatal(err)
			}
			if len(page.Entries) != len(prefix) {
				t.Fatalf("read %d events, want %d", len(page.Entries), len(prefix))
			}
			for index, entry := range page.Entries {
				if !proto.Equal(entry, prefix[index]) {
					t.Fatalf("event %d differs", index)
				}
			}
			db := postgres.Open(t, integrationDatabase.URL)
			var blocks int
			if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM session_event_block WHERE session_id=$1", sessionID).Scan(&blocks); err != nil {
				t.Fatal(err)
			}
			if (blocks > 0) != compact {
				t.Fatalf("compact=%t created %d blocks", compact, blocks)
			}
			for {
				summary, err := service.reads.GetSessionSummary(callContext(t), &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
				if err != nil {
					t.Fatal(err)
				}
				if summary.GetFoldedThrough() == uint64(len(prefix)) {
					break
				}
				if _, err := watch.Recv(); err != nil {
					t.Fatal(err)
				}
			}
			var delta *eventlogpb.EventEntry
			for _, entry := range prefix {
				if entry.Event.GetTextDelta() != nil {
					delta = entry
					break
				}
			}
			key := &historypb.EntityKey{EntityKind: historypb.ThreadEntityKind_THREAD_ENTITY_KIND_ITEM, EntityId: delta.Event.GetTextDelta().GetItemId()}
			entities, err := service.reads.ReadThreadEntities(callContext(t), &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: &historypb.ReadThreadEntitiesRequest_Keys{Keys: &historypb.EntityKeys{Keys: []*historypb.EntityKey{key}}}})
			if err != nil {
				t.Fatal(err)
			}
			frames, err := service.reads.ReadNativeFrames(callContext(t), &historypb.ReadNativeFramesRequest{SessionId: sessionID.String(), ProjectionEpoch: entities.ProjectionEpoch, Entity: key, ObservationCursor: delta.Cursor, Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(frames.Frames) != 1 || !proto.Equal(frames.Frames[0].Entry, prefix[delta.Event.SourceSequences[0]-1]) {
				t.Fatalf("native evidence differs: %v", frames)
			}
		})
	}
}

func TestEachMethodEnforcesItsCallerContract(t *testing.T) {
	service := start(t, nil)
	_, err := service.reads.GetSessionSummary(callContext(t), &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	// The exact App service SVID has the read role; an unknown Session stays hidden.
	if got := status.Code(err); got != codes.NotFound {
		t.Errorf("a missing Session read: %v, want NotFound", got)
	}
	pushCtx := metadata.AppendToOutgoingContext(callContext(t), "authorization", "Bearer "+readerToken)
	push, err := service.ingress.Push(pushCtx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := push.Recv(); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a reader on the ingress: %v, want Unauthenticated", err)
	}
}

func TestShuttingDownEndsOpenWatchesWithReconnectRequired(t *testing.T) {
	service := start(t, nil)
	watch, err := service.reads.WatchSessionSummaries(callContext(t), &historypb.WatchSessionSummariesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if response, err := watch.Recv(); err != nil || response.GetChanged() == nil {
		t.Fatalf("the watch did not open: %v, %v", response, err)
	}
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Fatalf("serve returned %v, want context.Canceled", err)
	}
	if response, err := watch.Recv(); err != nil || response.GetReconnectRequired() == nil {
		t.Fatalf("want reconnect_required, received %v, %v", response, err)
	}
	if _, err := watch.Recv(); err != io.EOF {
		t.Fatalf("want the watch to end cleanly, received %v", err)
	}
}

func TestTheReadDatabaseRefusesWrites(t *testing.T) {
	url, err := readOnly(integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("postgres", url)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	// A write that would change no rows is refused all the same.
	_, err = db.ExecContext(t.Context(), "DELETE FROM session_history WHERE false")
	var failure *pq.Error
	if !errors.As(err, &failure) || failure.Code != "25006" {
		t.Errorf("want read_only_sql_transaction (25006), got %v", err)
	}
}

func TestADatabaseURLThatSetsOptionsIsRefusedWithoutEchoingIt(t *testing.T) {
	const secret = "test-password-never-printed"
	_, err := readOnly("postgres://test-user:" + secret + "@test-database/history?options=-c%20statement_timeout%3D1")
	if err == nil || strings.Contains(err.Error(), secret) {
		t.Errorf("readOnly returned %v, want a refusal that leaves out the URL", err)
	}
}

// A health stream starts with the service's current status and continues with each change.
func watchHealth(ctx context.Context, t *testing.T, service running, name string) healthpb.Health_WatchClient {
	t.Helper()
	watch, err := service.health.Watch(ctx, &healthpb.HealthCheckRequest{Service: name})
	if err != nil {
		t.Fatal(err)
	}
	return watch
}

func awaitHealth(t *testing.T, watch healthpb.Health_WatchClient, want healthpb.HealthCheckResponse_ServingStatus) {
	t.Helper()
	for {
		response, err := watch.Recv()
		if err != nil {
			t.Fatalf("the health stream ended before reporting %v: %v", want, err)
		}
		if response.GetStatus() == want {
			return
		}
	}
}

func TestTheServiceReportsServingOnceItsFoldRuns(t *testing.T) {
	service := start(t, nil)
	awaitHealth(t, watchHealth(callContext(t), t, service, projection.FoldHealthService), healthpb.HealthCheckResponse_SERVING)
	if health, err := service.health.Check(t.Context(), &healthpb.HealthCheckRequest{}); err != nil || health.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Errorf("overall health: %v, %v", health, err)
	}
}

func TestTheServiceReportsNotServingWhenToldToStop(t *testing.T) {
	service := start(t, nil)
	ctx, cancel := context.WithCancel(callContext(t))
	defer cancel()
	watch := watchHealth(ctx, t, service, "")
	awaitHealth(t, watch, healthpb.HealthCheckResponse_SERVING)
	stopped := make(chan error, 1)
	go func() { stopped <- service.stop() }()
	awaitHealth(t, watch, healthpb.HealthCheckResponse_NOT_SERVING)
	// An open health stream would hold the graceful stop until its grace ran out.
	cancel()
	if err := <-stopped; !errors.Is(err, context.Canceled) {
		t.Fatalf("serve returned %v, want context.Canceled", err)
	}
}

func TestAWatchRenewsAtTheConfiguredLease(t *testing.T) {
	service := start(t, func(c *config) { c.WatchLeaseSeconds = 0.2 })
	// A watch that kept the default lease, or took the request timeout's, would not end within this.
	ctx, cancel := context.WithTimeout(callContext(t), 5*time.Second)
	defer cancel()
	watch, err := service.reads.WatchSessionSummaries(ctx, &historypb.WatchSessionSummariesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	for {
		response, err := watch.Recv()
		if err != nil {
			t.Fatalf("the watch ended without reconnect_required: %v", err)
		}
		if response.GetReconnectRequired() != nil {
			return
		}
	}
}

func TestAListenerThatFailsEndsTheServiceWithItsError(t *testing.T) {
	c := svidConfig(t, peertlstesting.NewCA(t))
	closed, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed.Close()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	if err := serve(ctx, c, closed); !errors.Is(err, net.ErrClosed) {
		t.Errorf("serve returned %v, want the listener's error", err)
	}
}

func TestACallThatOutlastsTheConfiguredRequestTimeoutIsAbandoned(t *testing.T) {
	// Any positive timeout has run out by the time the call reaches the database.
	service := start(t, func(c *config) { c.RequestTimeoutSeconds = 1e-9 })
	_, err := service.reads.GetSessionSummary(callContext(t), &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	if got := status.Code(err); got != codes.DeadlineExceeded {
		t.Errorf("%v, want DeadlineExceeded", got)
	}
}

func duplicateListenerFD(t *testing.T, listener net.Listener) int {
	t.Helper()
	tcpListener, ok := listener.(*net.TCPListener)
	if !ok {
		t.Fatalf("listener %T is not a TCP listener", listener)
	}
	file, err := tcpListener.File()
	if err != nil {
		t.Fatal(err)
	}
	fd, dupErr := syscall.Dup(int(file.Fd()))
	closeErr := file.Close()
	if dupErr != nil {
		t.Fatal(dupErr)
	}
	if closeErr != nil {
		syscall.Close(fd)
		t.Fatal(closeErr)
	}
	return fd
}

// run is what main serves: the settings name the addresses, and the Kubernetes client is built from the kubeconfig.
func TestRunServesTheReadAPIAndTheIngressOnOneConfiguredListener(t *testing.T) {
	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	// Workload identities are resolved lazily; this test makes no authenticated workload call.
	const unreachableCluster = `{"apiVersion": "v1", "kind": "Config", "current-context": "test",
		"clusters": [{"name": "test", "cluster": {"server": "http://127.0.0.1:1"}}],
		"contexts": [{"name": "test", "context": {"cluster": "test", "user": "test"}}],
		"users": [{"name": "test", "user": {}}]}`
	if err := os.WriteFile(kubeconfig, []byte(unreachableCluster), 0o600); err != nil {
		t.Fatal(err)
	}
	readerListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readerListener.Close() })
	readerAddress := readerListener.Addr().(*net.TCPAddr)
	readerFD := duplicateListenerFD(t, readerListener)
	ca := peertlstesting.NewCA(t)
	c := svidConfig(t, ca)
	c.RPC.ListenAddress = fmt.Sprintf("fd:%d", readerFD)
	c.Kubeconfig = kubeconfig
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- run(ctx, c) }()
	t.Cleanup(func() {
		cancel()
		<-served
	})
	readTarget := net.JoinHostPort("127.0.0.1", strconv.Itoa(readerAddress.Port))
	clientTLS := peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.HistoryIngressURI}
	security, err := clientTLS.Credentials()
	if err != nil {
		t.Fatal(err)
	}
	dial := func(target string) *grpc.ClientConn {
		connection, err := grpc.NewClient(target, grpc.WithTransportCredentials(security))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { connection.Close() })
		return connection
	}
	call, stop := context.WithTimeout(t.Context(), callDeadline)
	defer stop()
	if _, err := healthpb.NewHealthClient(dial(readTarget)).Check(call, &healthpb.HealthCheckRequest{}, grpc.WaitForReady(true)); err != nil {
		t.Fatalf("the read port does not answer: %v", err)
	}
	// The service owns a duplicate, so releasing the original socket does not interrupt either API.
	readerListener.Close()
	push := func(target string) error {
		stream, err := session_ingresspb.NewSessionJournalIngressClient(dial(target)).Push(call, grpc.WaitForReady(true))
		if err != nil {
			return err
		}
		_, err = stream.Recv()
		return err
	}
	if got := status.Code(push(readTarget)); got != codes.Unauthenticated {
		t.Errorf("Push without a client certificate: %v, want Unauthenticated", got)
	}
}
