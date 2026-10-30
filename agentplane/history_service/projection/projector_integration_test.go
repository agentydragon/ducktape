package projection

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/lease"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const testPageSize = 8

var (
	testWriter = storage.Writer{Namespace: "test-sandbox-namespace", Name: "test-sandbox-runner", UID: "test-runner-account-uid"}
	testOrigin = time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
)

// at is when the entry at cursor happened.
func at(cursor uint64) time.Time { return testOrigin.Add(time.Duration(cursor) * time.Second) }

func stamped(entries []*eventlogpb.EventEntry) []*eventlogpb.EventEntry {
	for _, entry := range entries {
		if entry.Event.At == nil {
			entry.Event.At = timestamppb.New(at(entry.Cursor))
		}
	}
	return entries
}

func turnCompleted(cursor uint64, status eventpb.TurnStatus) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
		TurnCompleted: &eventpb.TurnCompleted{TurnId: "test-turn", Status: status},
	}})
}

func harnessExited(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_HarnessExited{HarnessExited: &eventpb.HarnessExited{}}})
}

// completedTurn is one turn whose reply streams as deltas for more than a page.
func completedTurn(first uint64) []*eventlogpb.EventEntry {
	const deltas = testPageSize + 2
	entries := []*eventlogpb.EventEntry{turnStarted(first), replyStarted(first + 1)}
	for index := uint64(0); index < deltas; index++ {
		entries = append(entries, replyDelta(first+2+index, "x"))
	}
	text := make([]byte, deltas)
	for index := range text {
		text[index] = 'x'
	}
	return append(entries, replyCompleted(first+2+deltas, string(text)),
		turnCompleted(first+3+deltas, eventpb.TurnStatus_TURN_STATUS_COMPLETED))
}

type projectorFixture struct {
	db        *sql.DB
	history   *storage.Store
	projector *Projector
}

func newProjectorFixture(t *testing.T) projectorFixture {
	t.Helper()
	db := testDB(t)
	history := storage.NewStore(db, false)
	return projectorFixture{db, history, &Projector{DB: db, History: history, Epoch: testEpoch, PageSize: testPageSize}}
}

// retained binds a new Session and gives it a summary row, as the fold's discovery does.
func (f projectorFixture) retained(t *testing.T, history *storage.Store, entries ...*eventlogpb.EventEntry) uuid.UUID {
	t.Helper()
	sessionID := uuid.New()
	if _, err := history.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec("INSERT INTO thread_history_summary (thread_id) VALUES ($1)", sessionID); err != nil {
		t.Fatal(err)
	}
	f.appendTo(t, history, sessionID, entries...)
	return sessionID
}

func (f projectorFixture) appendTo(t *testing.T, history *storage.Store, sessionID uuid.UUID, entries ...*eventlogpb.EventEntry) {
	t.Helper()
	if len(entries) == 0 {
		return
	}
	if _, err := history.Append(t.Context(), sessionID, stamped(entries)); err != nil {
		t.Fatal(err)
	}
}

func (f projectorFixture) acquire(t *testing.T, sessionID uuid.UUID) Lease {
	t.Helper()
	var lease *Lease
	if err := withTx(t, f.db, func(tx *sql.Tx) (err error) {
		lease, err = AcquireLease(t.Context(), tx, sessionID, time.Minute)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if lease == nil {
		t.Fatalf("lease for %s is held", sessionID)
	}
	return *lease
}

// fold projects one page of the Session.
func (f projectorFixture) fold(t *testing.T, sessionID uuid.UUID, lease Lease) Progress {
	t.Helper()
	progress, err := f.projector.ProjectBatch(t.Context(), sessionID, lease)
	if err != nil {
		t.Fatal(err)
	}
	return progress
}

func (f projectorFixture) summary(t *testing.T, sessionID uuid.UUID) *summary {
	t.Helper()
	var found *summary
	if err := withTx(t, f.db, func(tx *sql.Tx) (err error) {
		found, err = loadSummary(t.Context(), tx, sessionID)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if found == nil {
		t.Fatalf("no summary for %s", sessionID)
	}
	return found
}

// ended is the Session's `end` document: nil while its stream continues and `{}` once it ended cleanly.
func (f projectorFixture) ended(t *testing.T, sessionID uuid.UUID) *string {
	t.Helper()
	var document *string
	if err := f.db.QueryRow(`SELECT "end"::text FROM thread_history_summary WHERE thread_id = $1`, sessionID).Scan(&document); err != nil {
		t.Fatal(err)
	}
	return document
}

func attachedFeed(sessionID uuid.UUID, attached *runnerpb.Attached) *historypb.SessionFeedState {
	attached.SessionId = sessionID.String()
	return &historypb.SessionFeedState{Attached: attached}
}

func (f projectorFixture) recordFeed(t *testing.T, sessionID uuid.UUID, attached *runnerpb.Attached) {
	t.Helper()
	if err := f.history.RecordFeedState(t.Context(), sessionID, attachedFeed(sessionID, attached)); err != nil {
		t.Fatal(err)
	}
}

func TestProjectBatchFoldsAPageAtATimeUntilCaughtUp(t *testing.T) {
	f := newProjectorFixture(t)
	entries := append([]*eventlogpb.EventEntry{harnessStarted(1)}, completedTurn(2)...)
	total := uint64(len(entries))
	folded := f.retained(t, f.history, entries...)
	empty := f.retained(t, f.history)
	foldedLease, emptyLease := f.acquire(t, folded), f.acquire(t, empty)

	if got, want := f.fold(t, folded, foldedLease), (Progress{ThroughCursor: testPageSize, CommittedCursor: total}); got != want {
		t.Fatalf("first page = %+v, want %+v", got, want)
	}
	if got, want := f.fold(t, folded, foldedLease), (Progress{ThroughCursor: total, CommittedCursor: total}); got != want {
		t.Fatalf("second page = %+v, want %+v", got, want)
	}
	if got := f.fold(t, empty, emptyLease); got != (Progress{}) {
		t.Fatalf("empty Session = %+v, want no progress", got)
	}

	reply := entities(t, f.db, folded)["item/test-reply"]
	if completion := completionOf(t, reply); completion == nil || *completion != "text" {
		t.Fatalf("reply = %+v, want a completed item", reply)
	}
	if _, through := checkpointOf(t, f.db, folded); through != total {
		t.Fatalf("checkpoint through %d, want %d", through, total)
	}
	if count(t, f.db, "SELECT count(*) FROM thread_checkpoint WHERE thread_id = $1", empty) != 0 {
		t.Fatal("a Session with no Events got a checkpoint")
	}
	summary := f.summary(t, folded)
	if summary.lastTurnStatus.Int64 != int64(eventpb.TurnStatus_TURN_STATUS_COMPLETED) ||
		!sameTime(summary.firstEventAt, sql.NullTime{Time: at(1), Valid: true}) ||
		!sameTime(summary.lastEventAt, sql.NullTime{Time: at(total), Valid: true}) {
		t.Fatalf("summary = %+v", summary)
	}
}

func TestASessionIsIdleFromItsHarnessStartOrLastTurnEndUntilTheNextTurnOrStop(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history)
	lease := f.acquire(t, sessionID)
	idleSince := func(entries ...*eventlogpb.EventEntry) sql.NullTime {
		f.appendTo(t, f.history, sessionID, entries...)
		f.fold(t, sessionID, lease)
		return f.summary(t, sessionID).idleSince
	}
	idleAt := func(cursor uint64) sql.NullTime { return sql.NullTime{Time: at(cursor), Valid: true} }

	for step, want := range []struct {
		entry *eventlogpb.EventEntry
		idle  sql.NullTime
	}{
		{harnessStarted(1), idleAt(1)},
		{turnStarted(2), sql.NullTime{}},
		{replyStarted(3), sql.NullTime{}},
		{turnCompleted(4, eventpb.TurnStatus_TURN_STATUS_FAILED), idleAt(4)},
		// A late delta neither starts a turn nor stops the harness.
		{replyDelta(5, "x"), idleAt(4)},
		{harnessExited(6), sql.NullTime{}},
	} {
		if got := idleSince(want.entry); !sameTime(got, want.idle) {
			t.Fatalf("idle_since after step %d = %+v, want %+v", step+1, got, want.idle)
		}
	}
}

func TestModelActivityIsTheLastEventThatCarriesModelOutput(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1), replyDelta(2, "x"), replyDelta(3, ""), replyDelta(4, "y"), harnessStarted(5))
	f.fold(t, sessionID, f.acquire(t, sessionID))

	var activity time.Time
	if err := f.db.QueryRow("SELECT last_model_activity_at FROM thread_history_summary WHERE thread_id = $1", sessionID).Scan(&activity); err != nil {
		t.Fatal(err)
	}
	if !activity.Equal(at(4)) {
		t.Fatalf("last_model_activity_at = %v, want %v", activity, at(4))
	}
}

func TestASessionEndsOnlyOnceItsHarnessLaunchHasAnOutcome(t *testing.T) {
	f := newProjectorFixture(t)
	// The runner announces a Session whose setup script runs before its harness starts.
	sessionID := f.retained(t, f.history, entry(1, &eventpb.Event{Observation: &eventpb.Event_SetupStarted{SetupStarted: &eventpb.SetupStarted{}}}))
	f.recordFeed(t, sessionID, &runnerpb.Attached{
		HarnessState:  runnerpb.HarnessState_HARNESS_STATE_STOPPED,
		SetupState:    runnerpb.SetupState_SETUP_STATE_RUNNING,
		LaunchOutcome: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
	})
	lease := f.acquire(t, sessionID)

	f.fold(t, sessionID, lease)
	if ended := f.ended(t, sessionID); ended != nil {
		t.Fatalf("end = %s while setup runs, want none", *ended)
	}
	// Setup succeeded; the harness is still launching.
	f.appendTo(t, f.history, sessionID, entry(2, &eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{}}}))
	f.fold(t, sessionID, lease)
	if ended := f.ended(t, sessionID); ended != nil {
		t.Fatalf("end = %s while the harness launches, want none", *ended)
	}
	f.appendTo(t, f.history, sessionID, entry(3, &eventpb.Event{Observation: &eventpb.Event_HarnessLaunchFailed{
		HarnessLaunchFailed: &eventpb.HarnessLaunchFailed{Reason: "test failure"},
	}}))
	f.fold(t, sessionID, lease)
	if ended := f.ended(t, sessionID); ended == nil || *ended != "{}" {
		t.Fatalf("end = %v after the launch failed, want {}", ended)
	}
}

func TestAnAdmittedInputKeepsADormantSessionOpenUntilItsStartupOutcome(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1), harnessExited(2))
	f.recordFeed(t, sessionID, &runnerpb.Attached{
		HarnessState:  runnerpb.HarnessState_HARNESS_STATE_STOPPED,
		LaunchOutcome: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
	})
	lease := f.acquire(t, sessionID)
	ended := func(entries ...*eventlogpb.EventEntry) *string {
		f.appendTo(t, f.history, sessionID, entries...)
		f.fold(t, sessionID, lease)
		return f.ended(t, sessionID)
	}

	if got := ended(); got == nil || *got != "{}" {
		t.Fatalf("end = %v for a stopped harness, want {}", got)
	}
	admitted := entry(3, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{
		Command: &commandpb.Command{
			CommandId: "wake", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "continue"}},
		},
	}}})
	if got := ended(admitted); got != nil {
		t.Fatalf("end = %s with an admitted input, want none", *got)
	}
	launchFailed := entry(4, &eventpb.Event{Observation: &eventpb.Event_HarnessLaunchFailed{HarnessLaunchFailed: &eventpb.HarnessLaunchFailed{Reason: "failed"}}})
	if got := ended(launchFailed); got != nil {
		t.Fatalf("end = %s while the input has no outcome, want none", *got)
	}
	commandFailed := entry(5, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{CommandFailed: &eventpb.CommandFailed{CommandId: "wake", Reason: "failed"}}})
	if got := ended(commandFailed); got == nil || *got != "{}" {
		t.Fatalf("end = %v once the input failed, want {}", got)
	}
}

func TestAnAdmittedSessionWithoutEventsGetsAnEmptyViewFromItsAttachment(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history)
	lease := f.acquire(t, sessionID)

	f.fold(t, sessionID, lease)
	if count(t, f.db, "SELECT count(*) FROM thread_checkpoint WHERE thread_id = $1", sessionID) != 0 {
		t.Fatal("a catalog row alone gave the Session a view")
	}

	f.recordFeed(t, sessionID, &runnerpb.Attached{
		HarnessState:  runnerpb.HarnessState_HARNESS_STATE_STOPPED,
		SetupState:    runnerpb.SetupState_SETUP_STATE_RUNNING,
		LaunchOutcome: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
	})
	f.fold(t, sessionID, lease)
	if _, through := checkpointOf(t, f.db, sessionID); through != 0 {
		t.Fatalf("empty view through %d, want 0", through)
	}
	if view, _ := viewStateOf(t, f.db, sessionID); view.Operational.Status != threadfold.StatusActive {
		t.Fatalf("operational = %+v", view.Operational)
	}

	f.appendTo(t, f.history, sessionID, harnessStarted(1))
	if got, want := f.fold(t, sessionID, lease), (Progress{ThroughCursor: 1, CommittedCursor: 1}); got != want {
		t.Fatalf("first Event = %+v, want %+v", got, want)
	}
}

// removeEntries leaves the Session's history as a cleanup migration does: the entries deleted, their cursors removed.
func removeEntries(t *testing.T, db *sql.DB, sessionID uuid.UUID, ranges ...storage.CursorRange) {
	t.Helper()
	for _, removed := range ranges {
		if _, err := db.Exec("DELETE FROM session_event WHERE session_id = $1 AND cursor BETWEEN $2 AND $3",
			sessionID, removed.First, removed.Last); err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec("INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor) VALUES ($1, $2, $3)",
			sessionID, removed.First, removed.Last); err != nil {
			t.Fatal(err)
		}
	}
}

func TestTheFoldPassesOverRemovedCursorsIncludingThoseEndingTheHistory(t *testing.T) {
	f := newProjectorFixture(t)
	var entries []*eventlogpb.EventEntry
	for cursor := uint64(1); cursor <= 6; cursor++ {
		entries = append(entries, harnessStarted(cursor))
	}
	sessionID := f.retained(t, f.history, entries...)
	removeEntries(t, f.db, sessionID, storage.CursorRange{First: 2, Last: 2}, storage.CursorRange{First: 5, Last: 6})
	lease := f.acquire(t, sessionID)

	// The entries are folded first; the removed cursors after the last of them take a call of their own.
	if got, want := f.fold(t, sessionID, lease), (Progress{ThroughCursor: 4, CommittedCursor: 6}); got != want {
		t.Fatalf("entries = %+v, want %+v", got, want)
	}
	if got, want := f.fold(t, sessionID, lease), (Progress{ThroughCursor: 6, CommittedCursor: 6}); got != want {
		t.Fatalf("removed suffix = %+v, want %+v", got, want)
	}

	if _, through := checkpointOf(t, f.db, sessionID); through != 6 {
		t.Fatalf("checkpoint through %d, want 6", through)
	}
	if _, view := viewStateOf(t, f.db, sessionID); view.cursor != 6 || view.revision != 6 {
		t.Fatalf("view state at %d/%d, want 6/6", view.cursor, view.revision)
	}
	lifecycle := cursorsOf(t, f.db, "SELECT cursor FROM thread_entity WHERE thread_id = $1 AND entity_kind = 'lifecycle' ORDER BY cursor", sessionID)
	if !slices.Equal(lifecycle, []uint64{1, 3, 4}) {
		t.Fatalf("lifecycle entities at %v, want [1 3 4]", lifecycle)
	}
}

// foldedRows is each entity's identity, state and body, in the order the fold numbered them. A body's
// generation is the cursor that last wrote it, which a settled item's chunks no longer are.
func foldedRows(t *testing.T, db *sql.DB, sessionID uuid.UUID) []string {
	t.Helper()
	rows, err := db.Query(`SELECT entity_kind, entity_id, cursor, revision_cursor, pending, state::text, text_ref
		FROM thread_entity WHERE thread_id = $1 AND projection_epoch = $2 ORDER BY entity_index`, sessionID, testEpoch)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var folded []string
	for rows.Next() {
		var kind, id, state string
		var cursor, revision uint64
		var pending bool
		var textRef []byte
		if err := rows.Scan(&kind, &id, &cursor, &revision, &pending, &state, &textRef); err != nil {
			t.Fatal(err)
		}
		body := ""
		if textRef != nil {
			reference, err := threadfold.ParseReference(textRef)
			if err != nil {
				t.Fatal(err)
			}
			body = bodyOf(t, db, sessionID, reference)
		}
		folded = append(folded, fmt.Sprintf("%s/%s cursor=%d revision=%d pending=%t state=%s body=%q", kind, id, cursor, revision, pending, state, body))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return folded
}

func TestSettledHistoryFoldsToTheRowsOfTheFullLog(t *testing.T) {
	f := newProjectorFixture(t)
	// 1-2 start, 3-6 two chunk pairs, 7-8 completion.
	span := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}).Entries
	full := f.retained(t, f.history, span...)
	settledSpan := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}).Entries
	settled := f.retained(t, storage.NewStore(f.db, true), settledSpan...)

	for _, sessionID := range []uuid.UUID{full, settled} {
		lease := f.acquire(t, sessionID)
		for progress := f.fold(t, sessionID, lease); progress.ThroughCursor < progress.CommittedCursor; {
			progress = f.fold(t, sessionID, lease)
		}
		if _, through := checkpointOf(t, f.db, sessionID); through != uint64(len(span)) {
			t.Fatalf("checkpoint through %d, want %d", through, len(span))
		}
	}

	if got, want := foldedRows(t, f.db, settled), foldedRows(t, f.db, full); !slices.Equal(got, want) {
		t.Fatalf("settled rows = %q, want the full log's %q", got, want)
	}
	evidence := cursorsOf(t, f.db, "SELECT observation_cursor FROM thread_evidence WHERE thread_id = $1 ORDER BY observation_cursor", settled)
	native := cursorsOf(t, f.db, "SELECT source_sequence FROM thread_native_link WHERE thread_id = $1 ORDER BY source_sequence", settled)
	for _, cursor := range slices.Concat(evidence, native) {
		if cursor >= 3 && cursor <= 6 {
			t.Fatalf("settled history cites cursor %d, evidence %v native %v", cursor, evidence, native)
		}
	}
}

func TestSettlementAfterALiveFoldDropsTheChunksEvidence(t *testing.T) {
	f := newProjectorFixture(t)
	settling := storage.NewStore(f.db, true)
	span := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}).Entries
	sessionID := f.retained(t, settling, span[:6]...)
	lease := f.acquire(t, sessionID)
	f.fold(t, sessionID, lease)

	evidenceQuery := "SELECT observation_cursor FROM thread_evidence WHERE thread_id = $1 ORDER BY observation_cursor"
	nativeQuery := "SELECT source_sequence FROM thread_native_link WHERE thread_id = $1 ORDER BY source_sequence"
	if evidence, native := cursorsOf(t, f.db, evidenceQuery, sessionID), cursorsOf(t, f.db, nativeQuery, sessionID); !slices.Equal(evidence, []uint64{2, 4, 6}) ||
		!slices.Equal(native, []uint64{1, 3, 5}) {
		t.Fatalf("live fold cites evidence %v native %v, want the chunks", evidence, native)
	}

	f.appendTo(t, settling, sessionID, span[6:]...)
	f.fold(t, sessionID, lease)
	evidence, native := cursorsOf(t, f.db, evidenceQuery, sessionID), cursorsOf(t, f.db, nativeQuery, sessionID)
	for _, cursor := range slices.Concat(evidence, native) {
		if cursor >= 3 && cursor <= 6 {
			t.Fatalf("settlement left cursor %d cited, evidence %v native %v", cursor, evidence, native)
		}
	}
	if !slices.Contains(evidence, 8) {
		t.Fatalf("evidence %v does not cite the completion", evidence)
	}
	if reply := entities(t, f.db, sessionID)["item/msg_test#0"]; reply.kind == "" || completionOf(t, reply) == nil {
		t.Fatalf("reply = %+v, want a completed item", reply)
	}
}

// appendRaw stores an entry the way a buggy or foreign writer could: without the checks Append makes.
func appendRaw(t *testing.T, db *sql.DB, sessionID uuid.UUID, raw *eventlogpb.EventEntry) {
	t.Helper()
	payload, err := proto.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO session_event (session_id, cursor, payload, kind) VALUES ($1, $2, $3, 0)", sessionID, raw.Cursor, payload); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE session_history SET last_cursor = $2 WHERE id = $1", sessionID, raw.Cursor); err != nil {
		t.Fatal(err)
	}
}

func TestHistoryTheFoldCannotApplyIsRetainedAsAFailureAtItsCursor(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1), harnessStarted(2))
	lease := f.acquire(t, sessionID)
	f.fold(t, sessionID, lease)

	other := harnessStarted(3)
	other.Origin.SourceId = "other-runner"
	appendRaw(t, f.db, sessionID, other)

	_, err := f.projector.ProjectBatch(t.Context(), sessionID, lease)
	var replicationError *ReplicationError
	if !errors.As(err, &replicationError) {
		t.Fatalf("a source change = %v, want a ReplicationError", err)
	}
	view, _ := viewStateOf(t, f.db, sessionID)
	if view.Operational.Status != threadfold.StatusFailed || view.Operational.FeedError == nil ||
		view.Operational.FeedError.Message != stalledMessage ||
		view.Operational.FeedError.Cursor == nil || *view.Operational.FeedError.Cursor != 3 {
		t.Fatalf("operational = %+v %+v, want the stall at cursor 3", view.Operational, view.Operational.FeedError)
	}
	if _, through := checkpointOf(t, f.db, sessionID); through != 2 {
		t.Fatalf("checkpoint through %d, want it kept at 2", through)
	}
}

func TestAnObservationTheFoldDoesNotModelStallsWithoutACursor(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1))
	lease := f.acquire(t, sessionID)
	f.fold(t, sessionID, lease)
	f.appendTo(t, f.history, sessionID, entry(2, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: "test-reply"},
	}}))

	_, err := f.projector.ProjectBatch(t.Context(), sessionID, lease)
	var notUnderstood *threadfold.ObservationNotUnderstoodError
	if !errors.As(err, &notUnderstood) {
		t.Fatalf("an item of no known kind = %v, want ObservationNotUnderstoodError", err)
	}
	view, _ := viewStateOf(t, f.db, sessionID)
	if view.Operational.Status != threadfold.StatusFailed || view.Operational.FeedError == nil || view.Operational.FeedError.Cursor != nil {
		t.Fatalf("operational = %+v %+v, want a stall naming no cursor", view.Operational, view.Operational.FeedError)
	}
}

func TestAProjectionWithoutItsLeaseFoldsNothing(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1))
	held := f.acquire(t, sessionID)
	if err := withTx(t, f.db, func(tx *sql.Tx) error { return ReleaseLease(context.Background(), tx, held) }); err != nil {
		t.Fatal(err)
	}

	if _, err := f.projector.ProjectBatch(t.Context(), sessionID, held); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("projecting without the lease = %v, want lease.ErrLost", err)
	}
	if count(t, f.db, "SELECT count(*) FROM thread_checkpoint WHERE thread_id = $1", sessionID) != 0 {
		t.Fatal("a projection without its lease advanced the fold")
	}
}

func TestAFeedSnapshotPastTheFoldedPrefixWaitsForTheFoldToReachIt(t *testing.T) {
	f := newProjectorFixture(t)
	var entries []*eventlogpb.EventEntry
	for cursor := uint64(1); cursor <= testPageSize+4; cursor++ {
		entries = append(entries, harnessStarted(cursor))
	}
	sessionID := f.retained(t, f.history, entries...)
	f.recordFeed(t, sessionID, &runnerpb.Attached{
		LastCursor:    uint64(len(entries)),
		HarnessState:  runnerpb.HarnessState_HARNESS_STATE_STOPPED,
		LaunchOutcome: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
	})
	lease := f.acquire(t, sessionID)

	f.fold(t, sessionID, lease)
	if f.summary(t, sessionID).attached != nil {
		t.Fatal("the view adopted a snapshot of entries the fold had not reached")
	}
	f.fold(t, sessionID, lease)
	if f.summary(t, sessionID).attached == nil {
		t.Fatal("the view never adopted the snapshot")
	}
}

func TestABatchFetchedBeforeAnotherAdvancedTheFoldIsRefused(t *testing.T) {
	f := newProjectorFixture(t)
	sessionID := f.retained(t, f.history, harnessStarted(1), harnessStarted(2))
	lease := f.acquire(t, sessionID)
	f.fold(t, sessionID, lease)

	// A batch that read the Session from the start, as the fold did before the batch above committed.
	if _, err := f.projector.projectPrefix(t.Context(), sessionID, lease, 0); !errors.Is(err, errCheckpointAdvanced) {
		t.Fatalf("a stale batch = %v, want errCheckpointAdvanced", err)
	}
	if _, through := checkpointOf(t, f.db, sessionID); through != 2 {
		t.Fatalf("checkpoint through %d, want 2", through)
	}
}
