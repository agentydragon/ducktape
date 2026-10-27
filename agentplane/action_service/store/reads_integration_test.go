package store

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

// submitAt submits a request that was created at the time, so a test names the order a listing returns.
func (f fixture) submitAt(t *testing.T, owner CallerPrincipal, key string, at time.Time) ActionRequest {
	t.Helper()
	request := f.submit(t, owner, key)
	f.exec(t, `UPDATE action_request SET created_at = $2, updated_at = $2 WHERE id = $1`, request.ID, at)
	return f.get(t, request.ID)
}

func ids(requests []ActionRequest) []uuid.UUID {
	ids := make([]uuid.UUID, len(requests))
	for i, request := range requests {
		ids[i] = request.ID
	}
	return ids
}

func (f fixture) list(t *testing.T, principal ReadPrincipal, filter ListFilter) []ActionRequest {
	t.Helper()
	requests, err := f.List(t.Context(), principal, filter)
	if err != nil {
		t.Fatal(err)
	}
	return requests
}

func TestListPagesNewestFirstAfterTheLastSeenRequest(t *testing.T) {
	f := newFixture(t)
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	var mine []ActionRequest
	// The middle two share a creation time: the ID breaks the tie in the same direction.
	for i, offset := range []time.Duration{0, time.Minute, time.Minute, 2 * time.Minute} {
		mine = append(mine, f.submitAt(t, caller, fmt.Sprintf("page-%d", i), base.Add(offset)))
	}
	foreign := f.submitAt(t, otherCaller, "page-foreign", base.Add(time.Hour))
	newestFirst := slices.Clone(mine)
	slices.SortFunc(newestFirst, func(a, b ActionRequest) int {
		return -cmp.Or(a.CreatedAt.Compare(b.CreatedAt), bytes.Compare(a.ID[:], b.ID[:]))
	})

	if got := f.list(t, caller, ListFilter{}); !slices.Equal(ids(got), ids(newestFirst)) {
		t.Errorf("a caller lists %v, want its own requests %v", ids(got), ids(newestFirst))
	}
	if got := f.list(t, operator, ListFilter{}); !slices.Equal(ids(got), append([]uuid.UUID{foreign.ID}, ids(newestFirst)...)) {
		t.Errorf("an operator lists %v", ids(got))
	}

	limit := 2
	var seen []uuid.UUID
	for {
		filter := ListFilter{Limit: &limit}
		if len(seen) > 0 {
			filter.AfterID = &seen[len(seen)-1]
		}
		page := f.list(t, caller, filter)
		seen = append(seen, ids(page)...)
		if len(page) < limit {
			break
		}
	}
	if !slices.Equal(seen, ids(newestFirst)) {
		t.Errorf("pages of two joined to %v, want %v", seen, ids(newestFirst))
	}

	// The position need not match the filters applied after it.
	oldest := f.decide(t, newestFirst[3], VerdictDeny)
	denied := []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DENIED}
	if got := f.list(t, caller, ListFilter{States: denied, AfterID: &newestFirst[0].ID}); !slices.Equal(ids(got), []uuid.UUID{oldest.ID}) {
		t.Errorf("denied requests after the newest = %v", ids(got))
	}
	if got := f.list(t, caller, ListFilter{States: denied, AfterID: &oldest.ID}); len(got) != 0 {
		t.Errorf("denied requests after the oldest = %v", ids(got))
	}
	if got := f.list(t, caller, ListFilter{States: []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_DENIED}}); !slices.Equal(ids(got), []uuid.UUID{oldest.ID}) {
		t.Errorf("allowed or denied requests = %v", ids(got))
	}
	if got := f.list(t, operator, ListFilter{AfterID: &foreign.ID}); !slices.Equal(ids(got), ids(newestFirst)) {
		t.Errorf("an operator lists after another owner's request: %v", ids(got))
	}
}

func TestListFindsRequestsByTheirKeyWithinTheReadersScope(t *testing.T) {
	f := newFixture(t)
	first := f.submit(t, caller, "shared-key")
	second := f.submit(t, otherCaller, "shared-key")
	f.submit(t, caller, "other-key")
	key := "shared-key"

	if got := f.list(t, caller, ListFilter{IdempotencyKey: &key}); !slices.Equal(ids(got), []uuid.UUID{first.ID}) {
		t.Errorf("a caller finds %v", ids(got))
	}
	if got := f.list(t, reader, ListFilter{IdempotencyKey: &key}); len(got) != 2 || !slices.Contains(ids(got), second.ID) {
		t.Errorf("a service reader finds %v", ids(got))
	}
}

func TestListRefusesWhatItCannotPage(t *testing.T) {
	f := newFixture(t)
	foreign := f.submit(t, otherCaller, "foreign")
	zero := 0
	unknown := uuid.New()

	for name, test := range map[string]struct {
		principal ReadPrincipal
		filter    ListFilter
		want      error
	}{
		"limit below one":        {caller, ListFilter{Limit: &zero}, ErrInvalid},
		"unknown state":          {caller, ListFilter{States: []action_servicepb.ActionState{99}}, ErrInvalid},
		"another owner's anchor": {caller, ListFilter{AfterID: &foreign.ID}, ErrNotFound},
		"unknown anchor":         {caller, ListFilter{AfterID: &unknown}, ErrNotFound},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := f.List(t.Context(), test.principal, test.filter); !errors.Is(err, test.want) {
				t.Errorf("got %v, want %v", err, test.want)
			}
		})
	}
}

func TestRequestsAreVisibleToTheirOwnerAndToReaders(t *testing.T) {
	f := newFixture(t)
	request := f.submit(t, caller, "visible")

	for name, principal := range map[string]ReadPrincipal{"owner": caller, "operator": operator, "service reader": reader} {
		if got, err := f.Get(t.Context(), request.ID, principal); err != nil || got.ID != request.ID {
			t.Errorf("%s: Get = %v, %v", name, got.ID, err)
		}
		if events, err := f.Events(t.Context(), request.ID, principal, EventsFilter{}); err != nil || len(events) != 1 {
			t.Errorf("%s: Events = %v, %v", name, events, err)
		}
	}
	for name, id := range map[string]uuid.UUID{"another owner's": request.ID, "an unknown": uuid.New()} {
		if _, err := f.Get(t.Context(), id, otherCaller); !errors.Is(err, ErrNotFound) {
			t.Errorf("Get of %s request = %v", name, err)
		}
		if _, err := f.Events(t.Context(), id, otherCaller, EventsFilter{}); !errors.Is(err, ErrNotFound) {
			t.Errorf("Events of %s request = %v", name, err)
		}
	}
	if got := f.list(t, otherCaller, ListFilter{}); len(got) != 0 {
		t.Errorf("another owner lists %v", ids(got))
	}
}

func TestRequestViewsDoNotDependOnTheReader(t *testing.T) {
	f := newFixture(t)
	body := input("unredacted")
	body.Arguments = json.RawMessage(`{"api_key": "test-api-key", "nested": {"password": "test-password"}, "list": [{"token": "test-token"}]}`)
	request := f.decide(t, f.submitInput(t, caller, body, Admission{RequestID: uuid.New()}), VerdictAllow)
	result := `{"access_token": "test-access-token", "rows": [1, 2]}`
	failure := `{"kind": "test_failure", "message": "test message", "client_secret": "test-client-secret"}`
	f.exec(t, `UPDATE action_execution SET state = 'failed', result = $2::jsonb, error = $3::jsonb, completed_at = now()
		WHERE request_id = $1`, request.ID, result, failure)

	want := f.get(t, request.ID)
	sameJSON(t, want.Arguments, body.Arguments)
	sameJSON(t, want.Execution.Result, []byte(result))
	sameJSON(t, want.Execution.Error, []byte(failure))
	if want.Caller != caller.Account {
		t.Errorf("caller = %+v", want.Caller)
	}
	for name, principal := range map[string]ReadPrincipal{"owner": caller, "operator": operator, "service reader": reader} {
		got, err := f.Get(t.Context(), request.ID, principal)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("%s reads %+v, %v; want %+v", name, got, err, want)
		}
		if listed := f.list(t, principal, ListFilter{}); len(listed) != 1 || !reflect.DeepEqual(listed[0], want) {
			t.Errorf("%s lists %+v", name, listed)
		}
		if page, _, err := f.History(t.Context(), principal, HistoryPage{Limit: 10}); err != nil || len(page) != 1 || !reflect.DeepEqual(page[0], want) {
			t.Errorf("%s reads history %+v, %v", name, page, err)
		}
	}
}

func TestHistoryHoldsACallersOwnRequestsOnly(t *testing.T) {
	f := newFixture(t)
	own := f.decide(t, f.submit(t, caller, "own-history"), VerdictDeny)
	other := f.decide(t, f.submit(t, otherCaller, "other-history"), VerdictDeny)
	for name, test := range map[string]struct {
		principal ReadPrincipal
		want      []uuid.UUID
	}{
		"the owner":        {caller, []uuid.UUID{own.ID}},
		"another owner":    {otherCaller, []uuid.UUID{other.ID}},
		"an operator":      {operator, []uuid.UUID{other.ID, own.ID}},
		"a service reader": {reader, []uuid.UUID{other.ID, own.ID}},
	} {
		page, _, err := f.History(t.Context(), test.principal, HistoryPage{Limit: 10})
		if got := ids(page); err != nil || !slices.Equal(got, test.want) {
			t.Errorf("%s reads history %v, %v; want %v", name, got, err, test.want)
		}
	}
}

// decidedAt submits and decides a request, then places its transition at the time, so a test names the order
// history returns.
func (f fixture) decidedAt(t *testing.T, key string, admission Admission, verdict Verdict, at time.Time) ActionRequest {
	t.Helper()
	request := f.submitInput(t, caller, input(key), admission)
	if request.State == action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING {
		request = f.decide(t, request, verdict)
	}
	f.exec(t, `UPDATE action_request SET history_at = $2 WHERE id = $1`, request.ID, at)
	return request
}

func vote(verdict Verdict) *ProviderVote {
	return &ProviderVote{Provider: "test-provider", Verdict: verdict, ReasonCode: "test_reason"}
}

func (f fixture) history(t *testing.T, page HistoryPage) ([]ActionRequest, *string) {
	t.Helper()
	requests, next, err := f.History(t.Context(), operator, page)
	if err != nil {
		t.Fatal(err)
	}
	return requests, next
}

func TestHistoryListsRequestsThatLeftPendingNewestTransitionFirst(t *testing.T) {
	f := newFixture(t)
	f.submit(t, caller, "history-pending")
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	operatorDenied := f.decidedAt(t, "history-denied", Admission{RequestID: uuid.New()}, VerdictDeny, base)
	operatorAllowed := f.decidedAt(t, "history-allowed", Admission{RequestID: uuid.New()}, VerdictAllow, base.Add(time.Minute))
	cancelled := f.submit(t, caller, "history-cancelled")
	if _, err := f.Cancel(t.Context(), cancelled.ID, caller); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE action_request SET history_at = $2 WHERE id = $1`, cancelled.ID, base.Add(2*time.Minute))
	// The two provider Decisions share a time: the ID breaks the tie in the same direction.
	autoAllowed := f.decidedAt(t, "history-auto-allowed", Admission{RequestID: uuid.New(), Vote: vote(VerdictAllow)}, "", base.Add(3*time.Minute))
	autoDenied := f.decidedAt(t, "history-auto-denied", Admission{RequestID: uuid.New(), Vote: vote(VerdictDeny)}, "", base.Add(3*time.Minute))

	all := []ActionRequest{operatorDenied, operatorAllowed, cancelled, autoAllowed, autoDenied}
	historyAt := func(request ActionRequest) time.Time {
		var at time.Time
		if err := f.db.QueryRowContext(t.Context(), `SELECT history_at FROM action_request WHERE id = $1`, request.ID).Scan(&at); err != nil {
			t.Fatal(err)
		}
		return at
	}
	newestFirst := func(requests []ActionRequest) []uuid.UUID {
		sorted := slices.Clone(requests)
		slices.SortFunc(sorted, func(a, b ActionRequest) int {
			return -cmp.Or(historyAt(a).Compare(historyAt(b)), bytes.Compare(a.ID[:], b.ID[:]))
		})
		return ids(sorted)
	}
	walk := func(page HistoryPage) []uuid.UUID {
		var seen []uuid.UUID
		for {
			requests, next := f.history(t, page)
			seen = append(seen, ids(requests)...)
			if next == nil {
				return seen
			}
			page.Cursor = next
		}
	}

	for _, limit := range []int{1, 2, 4, 5, 100} {
		if got := walk(HistoryPage{Limit: limit}); !slices.Equal(got, newestFirst(all)) {
			t.Errorf("pages of %d joined to %v, want %v", limit, got, newestFirst(all))
		}
	}
	// A page that ends exactly at the last request has no next page.
	if page, next := f.history(t, HistoryPage{Limit: 5}); len(page) != 5 || next != nil {
		t.Errorf("a full last page = %d requests, next %v", len(page), next)
	}
	page, next := f.history(t, HistoryPage{Limit: 4})
	if next == nil || *next != encodeCursor(historyPosition{At: historyAt(page[3]), ID: page[3].ID}) {
		t.Errorf("next cursor %v does not end at the last request", next)
	}

	// Requests a provider allowed are left out before the page fills.
	withoutAutoAllowed := []ActionRequest{operatorDenied, operatorAllowed, cancelled, autoDenied}
	if got := walk(HistoryPage{Limit: 2, ExcludeAutoAllowed: true}); !slices.Equal(got, newestFirst(withoutAutoAllowed)) {
		t.Errorf("without auto-allowed requests: %v, want %v", got, newestFirst(withoutAutoAllowed))
	}
	if page, next := f.history(t, HistoryPage{Limit: 4, ExcludeAutoAllowed: true}); len(page) != 4 || next != nil {
		t.Errorf("a page filled by the remaining requests = %d requests, next %v", len(page), next)
	}
}

func TestACancelledAllowedRequestKeepsItsPlaceInHistory(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "kept")
	before := f.count(t, `SELECT count(*) FROM action_request WHERE history_at = $1`, allowed.UpdatedAt)
	if _, err := f.Cancel(t.Context(), allowed.ID, caller); err != nil {
		t.Fatal(err)
	}
	if after := f.count(t, `SELECT count(*) FROM action_request WHERE history_at = $1`, allowed.UpdatedAt); before != 1 || after != 1 {
		t.Errorf("requests placed at the allow: %d before the cancellation, %d after", before, after)
	}
}

func TestHistoryContinuesAfterACursorPythonWrote(t *testing.T) {
	f := newFixture(t)
	python := loadGolden(t).HistoryCursors.Encoded[0]
	at, err := time.Parse(time.RFC3339Nano, python.HistoryAt)
	if err != nil {
		t.Fatal(err)
	}
	at = at.UTC()
	atCursor := f.decidedAt(t, "at-cursor", Admission{RequestID: uuid.MustParse(python.ID)}, VerdictDeny, at)
	f.decidedAt(t, "after-cursor", Admission{RequestID: uuid.New()}, VerdictDeny, at.Add(time.Second))
	before := f.decidedAt(t, "before-cursor", Admission{RequestID: uuid.New()}, VerdictDeny, at.Add(-time.Second))

	page, next := f.history(t, HistoryPage{Limit: 10, Cursor: &python.Cursor})
	if next != nil || !slices.Equal(ids(page), []uuid.UUID{before.ID}) {
		t.Errorf("after %s the history holds %v, want only the request before it; the request at the cursor is %s",
			python.Cursor, ids(page), atCursor.ID)
	}
}

func TestHistoryRefusesWhatItCannotPage(t *testing.T) {
	f := newFixture(t)
	invalidCursor := "invalid"
	for name, page := range map[string]HistoryPage{
		"limit below one": {Limit: 0},
		"invalid cursor":  {Limit: 1, Cursor: &invalidCursor},
	} {
		if _, _, err := f.History(t.Context(), operator, page); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestEventsPageBySequence(t *testing.T) {
	f := newFixture(t)
	request := f.allowed(t, caller, "events")
	cancelled, err := f.Cancel(t.Context(), request.ID, caller)
	if err != nil {
		t.Fatal(err)
	}

	events := f.events(t, request.ID)
	if got := eventStates(events); !slices.Equal(got, []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_CANCELLED}) {
		t.Fatalf("states = %v", got)
	}
	for i, event := range events {
		if event.Sequence != int64(i+1) {
			t.Errorf("event %d has sequence %d", i, event.Sequence)
		}
	}
	if last := events[2]; last.Sequence != cancelled.Request.Version || !last.At.Equal(cancelled.Request.UpdatedAt) ||
		(last.Actor.GetCaller().GetNamespace() != caller.Account.Namespace || last.Actor.GetCaller().GetName() != caller.Account.Name) {
		t.Errorf("the cancellation's event = %+v", last)
	}
	if events[0].Actor != nil || events[1].Actor != nil {
		t.Errorf("only a cancellation names an actor: %+v", events[:2])
	}

	one := 1
	for name, test := range map[string]struct {
		filter EventsFilter
		want   []int64
	}{
		"after the first":        {EventsFilter{AfterSequence: 1}, []int64{2, 3}},
		"after the last":         {EventsFilter{AfterSequence: 3}, nil},
		"limited":                {EventsFilter{Limit: &one}, []int64{1}},
		"after the first, limit": {EventsFilter{AfterSequence: 1, Limit: &one}, []int64{2}},
	} {
		got, err := f.Events(t.Context(), request.ID, operator, test.filter)
		var sequences []int64
		for _, event := range got {
			sequences = append(sequences, event.Sequence)
		}
		if err != nil || !slices.Equal(sequences, test.want) {
			t.Errorf("%s: sequences %v, %v; want %v", name, sequences, err, test.want)
		}
	}
	zero := 0
	for name, filter := range map[string]EventsFilter{"negative sequence": {AfterSequence: -1}, "limit below one": {Limit: &zero}} {
		if _, err := f.Events(t.Context(), request.ID, operator, filter); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestVersionsHoldTheReadableRequestsInTheStates(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "versions-pending")
	denied := f.decide(t, f.submit(t, caller, "versions-denied"), VerdictDeny)
	foreign := f.submit(t, otherCaller, "versions-foreign")
	for name, tc := range map[string]struct {
		principal ReadPrincipal
		states    []action_servicepb.ActionState
		want      map[uuid.UUID]int64
	}{
		"a caller's every state":    {caller, nil, map[uuid.UUID]int64{pending.ID: pending.Version, denied.ID: denied.Version}},
		"a caller's pending":        {caller, []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING}, map[uuid.UUID]int64{pending.ID: pending.Version}},
		"a reader's pending":        {reader, []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING}, map[uuid.UUID]int64{pending.ID: pending.Version, foreign.ID: foreign.Version}},
		"an operator's every state": {operator, nil, map[uuid.UUID]int64{pending.ID: pending.Version, denied.ID: denied.Version, foreign.ID: foreign.Version}},
	} {
		got, err := f.Versions(t.Context(), tc.principal, tc.states)
		if err != nil || !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: %v, %v; want %v", name, got, err, tc.want)
		}
	}
	if _, err := f.Versions(t.Context(), operator, []action_servicepb.ActionState{99}); !errors.Is(err, ErrInvalid) {
		t.Errorf("an unknown state: %v", err)
	}
}

func TestFindReturnsTheRequestsTheReaderMaySee(t *testing.T) {
	f := newFixture(t)
	mine := f.decide(t, f.submit(t, caller, "find-mine"), VerdictAllow)
	foreign := f.submit(t, otherCaller, "find-foreign")
	sorted := func(ids []uuid.UUID) []uuid.UUID {
		return slices.SortedFunc(slices.Values(ids), func(a, b uuid.UUID) int { return bytes.Compare(a[:], b[:]) })
	}
	asked := []uuid.UUID{mine.ID, foreign.ID, uuid.New()}
	for name, tc := range map[string]struct {
		principal ReadPrincipal
		want      []uuid.UUID
	}{
		"the owner":   {caller, []uuid.UUID{mine.ID}},
		"an operator": {operator, []uuid.UUID{mine.ID, foreign.ID}},
	} {
		found, err := f.Find(t.Context(), tc.principal, asked)
		if err != nil || !slices.Equal(sorted(ids(found)), sorted(tc.want)) {
			t.Errorf("%s finds %v, %v; want %v", name, ids(found), err, tc.want)
		}
	}
	found, err := f.Find(t.Context(), caller, []uuid.UUID{mine.ID})
	if err != nil || len(found) != 1 || !reflect.DeepEqual(found[0], f.get(t, mine.ID)) {
		t.Errorf("found %+v, %v; want the request as Get reads it", found, err)
	}
}
