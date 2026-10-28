package worker

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"log/slog"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

const (
	testNamespace  = "test-namespace"
	testSandbox    = "test-sandbox"
	testSandboxUID = "test-sandbox-uid"
	testSession    = "test-session"
)

// testOwner owns the inboxes: a Sandbox runs as the ServiceAccount of its own name.
var testOwner = store.ServiceAccount{Namespace: testNamespace, Name: testSandbox}

// testIncarnation is testOwner as the Sandbox the destinations name.
var testIncarnation = store.Owner{Account: testOwner, SandboxUID: testSandboxUID}

func ptr[T any](v T) *T { return &v }

func matchingSandbox() sandboxreader.View {
	return sandboxreader.View{Identity: sandboxreader.Identity{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}}
}

// fakeSandboxes serves the one Sandbox the destinations name, until a test makes it fail or replaces it.
type fakeSandboxes struct {
	mu      sync.Mutex
	view    sandboxreader.View
	err     error
	lookups int
}

func (s *fakeSandboxes) Get(_ context.Context, _ string) (sandboxreader.View, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lookups++
	if s.err != nil {
		return sandboxreader.View{}, s.err
	}
	return s.view, nil
}

func (s *fakeSandboxes) serve(view sandboxreader.View) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.view, s.err = view, nil
}

func (s *fakeSandboxes) fail(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.err = err
}

func (s *fakeSandboxes) lookupCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lookups
}

// fakeHistory is a Session's History: the entries a test adds, with the watermark and the cursors History says
// it dropped.
type fakeHistory struct {
	mu         sync.Mutex
	lastCursor uint64
	entries    map[uint64]*eventlogpb.EventEntry
	settled    []*historypb.CursorRange
	removed    []*historypb.CursorRange
	err        error
	requests   []*historypb.ReadSessionEventsRequest
}

func (h *fakeHistory) add(entries ...*eventlogpb.EventEntry) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.entries == nil {
		h.entries = map[uint64]*eventlogpb.EventEntry{}
	}
	for _, entry := range entries {
		h.entries[entry.GetCursor()] = entry
		h.lastCursor = max(h.lastCursor, entry.GetCursor())
	}
}

// drop makes History say it dropped the cursors first through last: as settled deltas, or as removed.
func (h *fakeHistory) drop(first, last uint64, removed bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	span := &historypb.CursorRange{First: first, Last: last}
	if removed {
		h.removed = append(h.removed, span)
	} else {
		h.settled = append(h.settled, span)
	}
}

func (h *fakeHistory) ReadSessionEvents(_ context.Context, in *historypb.ReadSessionEventsRequest, _ ...grpc.CallOption) (*historypb.ReadSessionEventsResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.requests = append(h.requests, proto.Clone(in).(*historypb.ReadSessionEventsRequest))
	if h.err != nil {
		return nil, h.err
	}
	limit := int(in.GetLimit())
	if limit == 0 {
		limit = 128
	}
	var cursors []uint64
	for cursor := range h.entries {
		if cursor > in.GetAfterCursor() {
			cursors = append(cursors, cursor)
		}
	}
	slices.Sort(cursors)
	response := &historypb.ReadSessionEventsResponse{LastCursor: h.lastCursor, Removed: h.removed}
	if len(h.settled) > 0 {
		response.Settlements = []*historypb.SettledDeltas{{Ranges: h.settled}}
	}
	for _, cursor := range cursors[:min(limit, len(cursors))] {
		response.Entries = append(response.Entries, h.entries[cursor])
	}
	return response, nil
}

func (h *fakeHistory) readRequests() []*historypb.ReadSessionEventsRequest {
	h.mu.Lock()
	defer h.mu.Unlock()
	return slices.Clone(h.requests)
}

// fakeDispatcher records what is submitted to it, and says the command is queued unless a test says otherwise.
type fakeDispatcher struct {
	mu       sync.Mutex
	requests []*dispatcherpb.SubmitInputRequest
	respond  func(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error)
}

func (d *fakeDispatcher) SubmitInput(ctx context.Context, in *dispatcherpb.SubmitInputRequest, _ ...grpc.CallOption) (*dispatcherpb.SubmitInputResponse, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.requests = append(d.requests, proto.Clone(in).(*dispatcherpb.SubmitInputRequest))
	if d.respond != nil {
		return d.respond(ctx, in)
	}
	return dispatched(dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER, nil), nil
}

func (d *fakeDispatcher) submitted() []*dispatcherpb.SubmitInputRequest {
	d.mu.Lock()
	defer d.mu.Unlock()
	return slices.Clone(d.requests)
}

func dispatched(state dispatcherpb.DeliveryState, reason *string) *dispatcherpb.SubmitInputResponse {
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{State: state, Reason: reason}}
}

// fakeProvider stands in for the Providers of the sources: with nothing to read, it postpones the subscription
// for an hour, unless a test says what it returns.
type fakeProvider struct {
	mu         sync.Mutex
	handle     func(ctx context.Context, claim store.Claim, due store.DueSubscription) error
	calls      []store.DueSubscription
	reconciled chan store.DueSubscription
}

func newFakeProvider() *fakeProvider {
	return &fakeProvider{reconciled: make(chan store.DueSubscription, 64)}
}

func (p *fakeProvider) Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	p.mu.Lock()
	p.calls = append(p.calls, due)
	handle := p.handle
	p.mu.Unlock()
	select {
	case p.reconciled <- due:
	default:
	}
	if handle != nil {
		return handle(ctx, claim, due)
	}
	return &ProviderDeferred{Until: time.Now().Add(time.Hour)}
}

func (p *fakeProvider) on(handle func(ctx context.Context, claim store.Claim, due store.DueSubscription) error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.handle = handle
}

func (p *fakeProvider) returns(err error) {
	p.on(func(context.Context, store.Claim, store.DueSubscription) error { return err })
}

func (p *fakeProvider) callCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.calls)
}

// harness is a Worker over real PostgreSQL, with fakes for what it calls out to.
type harness struct {
	db         *sql.DB
	store      *store.Store
	worker     *Worker
	settings   Settings
	sandboxes  *fakeSandboxes
	history    *fakeHistory
	dispatcher *fakeDispatcher
	actions    *fakeProvider
}

// newHarness starts from a database without the other tests' inboxes: the worker claims whichever inbox is due.
func newHarness(t *testing.T, settings Settings) *harness {
	t.Helper()
	return newHarnessWithDatabaseURL(t, database.URL, settings)
}

func newIsolatedHarness(t *testing.T, settings Settings) *harness {
	t.Helper()
	return newHarnessWithDatabaseURL(t, database.Clone(t), settings)
}

func newHarnessWithDatabaseURL(t *testing.T, databaseURL string, settings Settings) *harness {
	t.Helper()
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	h := &harness{
		db: db, settings: settings, sandboxes: &fakeSandboxes{view: matchingSandbox()},
		history: &fakeHistory{}, dispatcher: &fakeDispatcher{}, actions: newFakeProvider(),
	}
	h.exec(t, `TRUNCATE inbox CASCADE`)
	h.restart()
	return h
}

// restart is a new process over the same rows: nothing but them carries over.
func (h *harness) restart() {
	h.store = store.New(h.db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000})
	h.worker = &Worker{
		Store: h.store, Namespace: testNamespace, Sandboxes: h.sandboxes, History: h.history,
		Dispatcher: h.dispatcher, Actions: h.actions, Settings: h.settings,
	}
}

func (h *harness) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := h.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

// step works one inbox, which must be due.
func (h *harness) step(t *testing.T) {
	t.Helper()
	worked, err := h.worker.step(t.Context())
	if err != nil {
		t.Fatalf("step: %v", err)
	}
	if !worked {
		t.Fatal("no inbox was due")
	}
}

func destination() store.DestinationRef {
	return store.DestinationRef{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}
}

func (h *harness) subscribeTo(t *testing.T, key string, request uuid.UUID) store.Subscription {
	t.Helper()
	subscription, err := h.store.Subscribe(t.Context(), testOwner, store.Subscribe{
		DestinationRef: destination(), SessionID: testSession, IdempotencyKey: key,
		Source: store.ActionsSource{RequestID: request}, LifetimeDays: 7,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (h *harness) subscribe(t *testing.T, key string) store.Subscription {
	t.Helper()
	return h.subscribeTo(t, key, uuid.New())
}

// postponeSources leaves only notices and the destination to schedule the inbox.
func (h *harness) postponeSources(t *testing.T) {
	t.Helper()
	h.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour'`)
}

// addEvents commits events first through last of the subscription's Action request to its inbox, as a Provider
// does.
func (h *harness) addEvents(t *testing.T, subscription store.Subscription, first, last int64) {
	t.Helper()
	request := subscription.Source.(store.ActionsSource).RequestID
	for sequence := first; sequence <= last; sequence++ {
		tx, err := h.db.BeginTx(t.Context(), nil)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := tx.ExecContext(t.Context(), `SELECT 1 FROM inbox WHERE id = $1 FOR UPDATE`, subscription.InboxID); err != nil {
			t.Fatal(err)
		}
		if err := h.store.AppendEvent(t.Context(), tx, subscription.InboxID, subscription.ID,
			store.ActionsEvent{RequestID: request, Sequence: sequence}, json.RawMessage(`{}`)); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatal(err)
		}
	}
}

// refuseInboxWrites makes every later update of an inbox row fail with a database error, as the release of a claim
// does when the database goes away.
func (h *harness) refuseInboxWrites(t *testing.T) {
	t.Helper()
	h.exec(t, `
		CREATE FUNCTION refuse_inbox_write() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN RAISE EXCEPTION 'inbox writes are refused'; END $$`)
	h.exec(t, `CREATE TRIGGER refuse_inbox_write BEFORE UPDATE ON inbox FOR EACH ROW EXECUTE FUNCTION refuse_inbox_write()`)
	t.Cleanup(func() {
		// The test's context has ended by now.
		ctx := context.WithoutCancel(t.Context())
		for _, drop := range []string{`DROP TRIGGER refuse_inbox_write ON inbox`, `DROP FUNCTION refuse_inbox_write()`} {
			if _, err := h.db.ExecContext(ctx, drop); err != nil {
				t.Error(err)
			}
		}
	})
}

// logBuffer collects the lines a log handler writes.
type logBuffer struct {
	mu   sync.Mutex
	data bytes.Buffer
}

func (b *logBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.Write(p)
}

// records are the log lines as JSON objects.
func (b *logBuffer) records(t *testing.T) []map[string]any {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()
	var records []map[string]any
	for line := range strings.Lines(b.data.String()) {
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("log line is not JSON: %v: %s", err, line)
		}
		records = append(records, record)
	}
	return records
}

// captureLogs collects what the test logs through the default logger, as the service writes it.
func captureLogs(t *testing.T) *logBuffer {
	t.Helper()
	logs := &logBuffer{}
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return logs
}

// setEntryTime changes when the entry was committed.
func (h *harness) setEntryTime(t *testing.T, inboxID uuid.UUID, cursor int64, at time.Time) {
	t.Helper()
	h.exec(t, `UPDATE entry SET created_at = $3 WHERE inbox_id = $1 AND cursor = $2`, inboxID, cursor, at)
}

// makeDue makes the inbox claimable, as the passing of its deadline does.
func (h *harness) makeDue(t *testing.T, inboxID uuid.UUID) {
	t.Helper()
	h.exec(t, `UPDATE inbox SET next_attempt = now() - interval '1 second', claim_until = now() - interval '1 second' WHERE id = $1`, inboxID)
}

type inboxRow struct {
	retired       bool
	staleCheckAt  *time.Time
	nextAttempt   *time.Time
	deliveryError *string
	acknowledged  int64
	lastCursor    int64
}

func (h *harness) row(t *testing.T, inboxID uuid.UUID) inboxRow {
	t.Helper()
	var row inboxRow
	var stale, next sql.NullTime
	var deliveryError sql.NullString
	if err := h.db.QueryRowContext(t.Context(), `
		SELECT retired, stale_check_at, next_attempt, delivery_error, acknowledged, last_cursor FROM inbox WHERE id = $1`,
		inboxID).Scan(&row.retired, &stale, &next, &deliveryError, &row.acknowledged, &row.lastCursor); err != nil {
		t.Fatal(err)
	}
	if stale.Valid {
		row.staleCheckAt = &stale.Time
	}
	if next.Valid {
		row.nextAttempt = &next.Time
	}
	if deliveryError.Valid {
		row.deliveryError = &deliveryError.String
	}
	return row
}

func (h *harness) page(t *testing.T, inboxID uuid.UUID) store.InboxPage {
	t.Helper()
	page, err := h.store.Read(t.Context(), testIncarnation, inboxID, 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func (h *harness) notice(t *testing.T, inboxID uuid.UUID) store.Notice {
	t.Helper()
	notice := h.page(t, inboxID).Notice
	if notice == nil {
		t.Fatal("the inbox has no notice")
	}
	return *notice
}

func (h *harness) subscription(t *testing.T, id uuid.UUID) store.Subscription {
	t.Helper()
	subscription, err := h.store.GetSubscription(t.Context(), testIncarnation, id)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (h *harness) claim(t *testing.T) store.Claim {
	t.Helper()
	claim, err := h.store.Claim(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if claim == nil {
		t.Fatal("no inbox was due")
	}
	return *claim
}

func (h *harness) requireNoneDue(t *testing.T) {
	t.Helper()
	claim, err := h.store.Claim(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if claim != nil {
		t.Fatalf("inbox %s was due", claim.ID)
	}
}

// prepared claims the inbox of a subscription with `events` entries and prepares the notice naming them, with
// nothing else due.
func (h *harness) prepared(t *testing.T, events int64) (store.Claim, store.Subscription, store.Notice) {
	t.Helper()
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, events)
	claim := h.claim(t)
	notice, err := h.store.PrepareNotice(t.Context(), claim)
	if err != nil || notice == nil {
		t.Fatalf("PrepareNotice = %+v, %v", notice, err)
	}
	return claim, subscription, *notice
}

func requireEqual(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %+v\nwant %+v", got, want)
	}
}

func requireWithin(t *testing.T, name string, got *time.Time, earliest, latest time.Time) {
	t.Helper()
	const tolerance = 2 * time.Millisecond
	if got == nil {
		t.Fatalf("%s is nil, want between %s and %s", name, earliest, latest)
	}
	if got.Before(earliest.Add(-tolerance)) || got.After(latest.Add(tolerance)) {
		t.Fatalf("%s = %s, want between %s and %s", name, got, earliest, latest)
	}
}

func admittedEntry(cursor uint64, commandID, text string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
		CommandAdmitted: &eventpb.CommandAdmitted{Command: &commandpb.Command{
			CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}},
		}},
	}}}
}

func confirmedEntry(cursor uint64, commandID string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
		HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: []string{commandID}},
	}}}
}

func unrelatedEntry(cursor uint64) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: &eventpb.Event{Observation: &eventpb.Event_HarnessLost{
		HarnessLost: &eventpb.HarnessLost{},
	}}}
}
