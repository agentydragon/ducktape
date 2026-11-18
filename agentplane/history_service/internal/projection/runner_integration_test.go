package projection

import (
	"bytes"
	"context"
	"database/sql"
	"log/slog"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// emptyDatabase removes every Session and fold row, so a cycle sees only the Sessions its test retained.
func emptyDatabase(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`TRUNCATE session_history, thread_checkpoint, thread_entity, thread_payload_manifest,
		thread_payload_chunk, thread_evidence, thread_native_link, thread_history_summary, session_projection_lease CASCADE`); err != nil {
		t.Fatal(err)
	}
}

// discoverable is a Session in History that the fold has not yet found: it has no summary row.
func (f projectorFixture) discoverable(t *testing.T, history *storage.Store, entries ...*eventlogpb.EventEntry) uuid.UUID {
	t.Helper()
	sessionID := f.retained(t, history, entries...)
	if _, err := f.db.Exec("DELETE FROM thread_history_summary WHERE thread_id = $1", sessionID); err != nil {
		t.Fatal(err)
	}
	return sessionID
}

func (f projectorFixture) cycle(t *testing.T, runner *FoldRunner) Backlog {
	t.Helper()
	backlog, err := runner.Cycle(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	return backlog
}

func TestCycleFoldsRetainedSessionsAPageAtATimeUntilCaughtUp(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	entries := append([]*eventlogpb.EventEntry{harnessStarted(1)}, completedTurn(2)...)
	total := uint64(len(entries))
	folded := f.discoverable(t, f.history, entries...)
	empty := f.discoverable(t, f.history)
	runner := NewFoldRunner(f.db, f.history, 2, testPageSize)

	if got, want := f.cycle(t, runner), (Backlog{Sessions: 2, Behind: 1, EventsBehind: total - testPageSize}); got != want {
		t.Fatalf("first cycle = %+v, want %+v", got, want)
	}
	// The empty Session caught up in the first cycle and left the queue.
	if got, want := f.cycle(t, runner), (Backlog{Sessions: 1}); got != want {
		t.Fatalf("second cycle = %+v, want %+v", got, want)
	}
	if got, want := f.cycle(t, runner), (Backlog{}); got != want {
		t.Fatalf("third cycle = %+v, want %+v", got, want)
	}

	if sourceID, through := checkpointOf(t, f.db, folded); through != total || sourceID == nil {
		t.Fatalf("checkpoint through %d, want %d", through, total)
	}
	if postgres.Count(t, f.db, "SELECT count(*) FROM thread_checkpoint WHERE thread_id = $1", empty) != 0 {
		t.Fatal("a Session with no Events got a checkpoint")
	}
	if postgres.Count(t, f.db, "SELECT count(*) FROM thread_history_summary WHERE thread_id = $1", empty) != 1 {
		t.Fatal("discovery gave the empty Session no summary row")
	}
}

func TestReplicasSplitSessionsByLease(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	sessionID := f.discoverable(t, f.history, completedTurn(1)...)
	first := NewFoldRunner(f.db, f.history, 1, testPageSize)
	second := NewFoldRunner(f.db, f.history, 1, testPageSize)

	if got := f.cycle(t, first).Behind; got != 1 {
		t.Fatalf("first replica left %d Sessions behind, want 1", got)
	}
	// The first replica's lease is live, so the second leaves the Session alone.
	if got := f.cycle(t, second).Behind; got != 0 {
		t.Fatalf("second replica folded %d Sessions the first holds", got)
	}
	if _, through := checkpointOf(t, f.db, sessionID); through != testPageSize {
		t.Fatalf("checkpoint through %d, want one page", through)
	}
}

func leases(t *testing.T, db *sql.DB, sessionID uuid.UUID) int {
	t.Helper()
	return postgres.Count(t, db, "SELECT count(*) FROM session_projection_lease WHERE session_id = $1", sessionID)
}

func TestCycleFoldsOnlySessionsWhoseHistoryChanged(t *testing.T) {
	for name, scenario := range map[string]struct {
		change func(t *testing.T, f projectorFixture, sessionID uuid.UUID)
		folded func(t *testing.T, f projectorFixture, sessionID uuid.UUID) bool
	}{
		"appended": {
			change: func(t *testing.T, f projectorFixture, sessionID uuid.UUID) {
				f.appendTo(t, f.history, sessionID, harnessStarted(2))
			},
			folded: func(t *testing.T, f projectorFixture, sessionID uuid.UUID) bool {
				_, through := checkpointOf(t, f.db, sessionID)
				return through == 2
			},
		},
		"feed state": {
			change: func(t *testing.T, f projectorFixture, sessionID uuid.UUID) {
				f.recordFeed(t, sessionID, &sessionpb.Attached{LastCursor: 1})
			},
			folded: func(t *testing.T, f projectorFixture, sessionID uuid.UUID) bool {
				return f.summary(t, sessionID).attached != nil
			},
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newProjectorFixture(t)
			emptyDatabase(t, f.db)
			const idle = 5
			var sessionIDs []uuid.UUID
			for range idle + 1 {
				sessionIDs = append(sessionIDs, f.discoverable(t, f.history, harnessStarted(1)))
			}
			runner := NewFoldRunner(f.db, f.history, 2, testPageSize)
			if got, want := f.cycle(t, runner), (Backlog{Sessions: idle + 1}); got != want {
				t.Fatalf("first cycle = %+v, want %+v", got, want)
			}
			if got := f.cycle(t, runner); got != (Backlog{}) {
				t.Fatalf("cycle with nothing changed = %+v, want none", got)
			}
			if got := leases(t, f.db, sessionIDs[0]); got != 0 {
				t.Fatalf("a caught-up Session has %d leases, want them handed back", got)
			}

			changed := sessionIDs[0]
			scenario.change(t, f, changed)
			if got, want := f.cycle(t, runner), (Backlog{Sessions: 1}); got != want {
				t.Fatalf("cycle after one Session changed = %+v, want %+v", got, want)
			}
			if !scenario.folded(t, f, changed) {
				t.Fatal("the fold did not carry the change into the Session's view")
			}
		})
	}
}

func TestConcurrentReplicasFoldEachChangedSessionOnce(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	const sessions, replicas = 24, 4
	for range sessions {
		f.discoverable(t, f.history, harnessStarted(1))
	}
	runners := make([]*FoldRunner, replicas)
	for index := range runners {
		runners[index] = NewFoldRunner(f.db, f.history, 2, testPageSize)
	}
	// Each Session takes one page, so a claim of a Session another replica folded would show as an extra one.
	var claimed atomic.Int64
	for postgres.Count(t, f.db, "SELECT count(*) FROM session_fold_pending") > 0 {
		var group errgroup.Group
		for _, runner := range runners {
			group.Go(func() error {
				backlog, err := runner.Cycle(t.Context())
				claimed.Add(int64(backlog.Sessions))
				return err
			})
		}
		if err := group.Wait(); err != nil {
			t.Fatal(err)
		}
	}
	if got := claimed.Load(); got != sessions {
		t.Fatalf("replicas folded %d Sessions, want each of %d once", got, sessions)
	}
	if got := postgres.Count(t, f.db, "SELECT count(*) FROM thread_checkpoint WHERE through_cursor = 1"); got != sessions {
		t.Fatalf("%d Sessions folded through their history, want %d", got, sessions)
	}
}

func TestCycleHandsBackTheLeaseOfASessionNoLongerRetained(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	// More than a page, so the first cycle leaves it behind and leased.
	sessionID := f.discoverable(t, f.history, completedTurn(1)...)
	runner := NewFoldRunner(f.db, f.history, 1, testPageSize)
	f.cycle(t, runner)
	if leases(t, f.db, sessionID) != 1 {
		t.Fatal("the fold holds no lease on the Session it is behind on")
	}

	if _, err := f.db.Exec("TRUNCATE session_history CASCADE"); err != nil {
		t.Fatal(err)
	}
	f.cycle(t, runner)
	if postgres.Count(t, f.db, "SELECT count(*) FROM session_projection_lease WHERE session_id = $1", sessionID) != 0 {
		t.Fatal("the fold kept its lease on a Session that is no longer retained")
	}
}

// foldedThrough waits, on the notifications each fold commit sends, until the Session's checkpoint reaches cursor.
func foldedThrough(t *testing.T, db *sql.DB, sessionID uuid.UUID, cursor uint64) {
	t.Helper()
	listener := pq.NewListener(integrationDatabase.URL, time.Second, time.Minute, nil)
	defer listener.Close()
	if err := listener.Listen(ChannelSummaries); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	for {
		var through uint64
		err := db.QueryRow("SELECT through_cursor FROM thread_checkpoint WHERE thread_id = $1", sessionID).Scan(&through)
		if err == nil && through == cursor {
			return
		}
		select {
		case <-listener.Notify:
		case <-ctx.Done():
			t.Fatalf("checkpoint of %s did not reach %d: %v", sessionID, cursor, ctx.Err())
		}
	}
}

func TestRunFoldsWithoutWaitingOutTheIntervalAndHandsBackItsLeases(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	entries := completedTurn(1)
	sessionID := f.discoverable(t, f.history, entries...)
	runner := NewFoldRunner(f.db, f.history, 1, testPageSize)
	changes := make(chan struct{}, 1)
	status := health.NewServer()
	ctx, stop := context.WithCancel(t.Context())
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		runner.Run(ctx, time.Hour, changes, status)
	}()

	// More than a page behind: an interval wait between pages would outlast the timeout.
	foldedThrough(t, f.db, sessionID, uint64(len(entries)))
	f.appendTo(t, f.history, sessionID, harnessStarted(uint64(len(entries)+1)))
	changes <- struct{}{}
	foldedThrough(t, f.db, sessionID, uint64(len(entries)+1))

	stop()
	<-stopped
	var lease *Lease
	if err := withTx(t, f.db, func(tx *sql.Tx) (err error) {
		lease, err = AcquireLease(t.Context(), tx, sessionID, time.Minute)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if lease == nil {
		t.Fatal("the stopped fold still holds its lease")
	}
}

func TestAFoldBugStopsTheFoldAndReportsItWhileTheServerKeepsServing(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	f.discoverable(t, f.history, harnessStarted(1))
	// With no history to read from, folding a Session is a nil dereference.
	runner := NewFoldRunner(f.db, nil, 1, testPageSize)
	status := health.NewServer()

	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	runner.Run(ctx, time.Hour, make(chan struct{}), status)

	check := func(service string) healthpb.HealthCheckResponse_ServingStatus {
		response, err := status.Check(t.Context(), &healthpb.HealthCheckRequest{Service: service})
		if err != nil {
			t.Fatal(err)
		}
		return response.Status
	}
	if fold, whole := check(FoldHealthService), check(""); fold != healthpb.HealthCheckResponse_NOT_SERVING ||
		whole != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("health after a fold bug: fold %v, server %v", fold, whole)
	}
}

// This models a retained attachment written before execution_principal was reserved.
func TestPermanentAttachmentFailureParksOnlyItsPendingVersion(t *testing.T) {
	f := newProjectorFixture(t)
	emptyDatabase(t, f.db)
	failed := f.discoverable(t, f.history, harnessStarted(1))
	runner := NewFoldRunner(f.db, f.history, 2, testPageSize)
	f.cycle(t, runner)
	if _, err := f.db.Exec(`UPDATE thread_history_summary SET attached = '{"lastCursor":"1","executionPrincipal":{"account":{"namespace":"test","name":"old-writer"},"uid":"old-uid"}}'::jsonb WHERE thread_id=$1`, failed); err != nil {
		t.Fatal(err)
	}
	f.appendTo(t, f.history, failed, harnessStarted(2))
	healthy := f.discoverable(t, f.history, harnessStarted(1))
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	// A database failure while retaining the diagnostic must remain retryable, even
	// though the original decode failure is permanent.
	if _, err := f.db.Exec(`ALTER TABLE thread_entity ADD CONSTRAINT test_reject_failure
		CHECK (state #>> '{operational,status}' IS DISTINCT FROM 'THREAD_FEED_STATUS_FAILED') NOT VALID`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := f.db.Exec(`ALTER TABLE thread_entity DROP CONSTRAINT IF EXISTS test_reject_failure`); err != nil {
			t.Error(err)
		}
	})
	f.cycle(t, runner)
	if view, _ := viewStateOf(t, f.db, failed); view.Operational.Status != historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ACTIVE {
		t.Fatalf("failed diagnostic transaction changed operational state: %+v", view.Operational)
	}
	if _, err := f.db.Exec(`ALTER TABLE thread_entity DROP CONSTRAINT test_reject_failure`); err != nil {
		t.Fatal(err)
	}
	logs.Reset()
	f.cycle(t, runner)
	view, _ := viewStateOf(t, f.db, failed)
	if view.Operational.Status != historypb.ThreadFeedStatus_THREAD_FEED_STATUS_FAILED || view.Operational.FeedError == nil || view.Operational.FeedError.Message != stalledMessage {
		t.Fatalf("operational = %+v, want permanent projection failure", view.Operational)
	}
	if _, through := checkpointOf(t, f.db, failed); through != 1 {
		t.Fatalf("failed checkpoint=%d, want 1", through)
	}
	if _, through := checkpointOf(t, f.db, healthy); through != 1 {
		t.Fatalf("healthy checkpoint=%d, want 1", through)
	}
	if postgres.Count(t, f.db, "SELECT count(*) FROM session_event WHERE session_id=$1", failed) != 2 || postgres.Count(t, f.db, "SELECT count(*) FROM session_fold_pending WHERE session_id=$1", failed) != 1 {
		t.Fatal("permanent failure removed retained history or its pending mark")
	}
	if !strings.Contains(logs.String(), "executionPrincipal") {
		t.Fatalf("failure log lost the decoder cause: %s", logs.String())
	}
	// Unrelated history changes and periodic cycles must not retry this version.
	once := logs.Len()
	f.appendTo(t, f.history, healthy, harnessStarted(2))
	for range 3 {
		f.cycle(t, runner)
	}
	if logs.Len() != once {
		t.Fatalf("unchanged permanent failure logged again: %s", logs.String())
	}
	if _, through := checkpointOf(t, f.db, healthy); through != 2 {
		t.Fatalf("healthy checkpoint=%d, want 2", through)
	}
	second := NewFoldRunner(f.db, f.history, 2, testPageSize)
	if got := f.cycle(t, second).Sessions; got != 0 {
		t.Fatalf("another owner claimed %d leased Sessions", got)
	}
	// A newer pending version warrants one attempt even if it still cannot fold.
	f.recordFeed(t, failed, &sessionpb.Attached{LastCursor: 2})
	f.cycle(t, runner)
	if logs.Len() == once {
		t.Fatal("new pending version was never attempted")
	}
	twice := logs.Len()
	f.cycle(t, runner)
	if logs.Len() != twice {
		t.Fatal("new failed version was not parked")
	}
	// A new owner re-evaluates retained failures once, allowing a new reader image
	// to recover them without rewriting queue rows during deployment.
	runner.releaseAll(t.Context())
	f.cycle(t, second)
	if logs.Len() == twice {
		t.Fatal("new owner did not attempt the failed projection")
	}
	afterTakeover := logs.Len()
	f.cycle(t, second)
	if logs.Len() != afterTakeover {
		t.Fatal("new owner retried the same failed version")
	}
	second.releaseAll(t.Context())
}
