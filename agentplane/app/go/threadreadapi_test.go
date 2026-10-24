package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/structpb"
)

type threadReadStoreFake struct {
	rows []threads.Thread
}

func (f *threadReadStoreFake) GetThread(_ context.Context, id uuid.UUID) (*threads.Thread, error) {
	for _, row := range f.rows {
		if row.ID == id {
			copy := row
			return &copy, nil
		}
	}
	return nil, nil
}

func (f *threadReadStoreFake) ListPage(_ context.Context, limit int, after *threads.PageCursor, includeArchived bool) (threads.Page, error) {
	page := threads.Page{Threads: make([]threads.Thread, 0, limit)}
	for _, row := range f.rows {
		if !includeArchived && row.Archived {
			continue
		}
		if after != nil && !(row.CreatedAt.Before(after.CreatedAt) || (row.CreatedAt.Equal(after.CreatedAt) && row.ID.String() < after.ID.String())) {
			continue
		}
		if len(page.Threads) == limit {
			last := page.Threads[len(page.Threads)-1]
			page.Next = &threads.PageCursor{CreatedAt: last.CreatedAt, ID: last.ID}
			break
		}
		page.Threads = append(page.Threads, row)
	}
	return page, nil
}

func (f *threadReadStoreFake) Rename(_ context.Context, id uuid.UUID, name *string) (threads.Thread, error) {
	for index, row := range f.rows {
		if row.ID == id {
			f.rows[index].Name = name
			return f.rows[index], nil
		}
	}
	return threads.Thread{}, threads.ErrNotFound
}

func (f *threadReadStoreFake) Archive(_ context.Context, id uuid.UUID) (threads.Thread, error) {
	for index, row := range f.rows {
		if row.ID == id {
			f.rows[index].Archived = true
			return f.rows[index], nil
		}
	}
	return threads.Thread{}, threads.ErrNotFound
}

func (f *threadReadStoreFake) Unarchive(_ context.Context, id uuid.UUID) (threads.Thread, error) {
	for index, row := range f.rows {
		if row.ID == id {
			f.rows[index].Archived = false
			return f.rows[index], nil
		}
	}
	return threads.Thread{}, threads.ErrNotFound
}

type threadReadHistoryFake struct {
	summaries map[uuid.UUID]historydata.FoldSummary
	sandboxes map[uuid.UUID]string
}

type threadReadInventoryFake struct {
	sandbox *sandboxespb.Sandbox
	err     error
}

func (f *threadReadInventoryFake) Get(context.Context, string) (*sandboxespb.Sandbox, error) {
	return f.sandbox, f.err
}

type threadReadRunnerFake struct {
	status *runnerpb.RunnerStatus
	err    error
}

func (f *threadReadRunnerFake) RunnerStatus(context.Context, string) (*runnerpb.RunnerStatus, error) {
	return f.status, f.err
}

func (f *threadReadHistoryFake) GetSummary(_ context.Context, id uuid.UUID) (historydata.FoldSummary, error) {
	return f.summaries[id], nil
}

func (f *threadReadHistoryFake) BatchGetSummaries(_ context.Context, ids []uuid.UUID) (map[uuid.UUID]historydata.FoldSummary, error) {
	result := make(map[uuid.UUID]historydata.FoldSummary, len(ids))
	for _, id := range ids {
		result[id] = f.summaries[id]
	}
	return result, nil
}

func (f *threadReadHistoryFake) FindSessions(_ context.Context, sandboxName, sandboxNameContains *string) (map[uuid.UUID]string, error) {
	result := make(map[uuid.UUID]string)
	for id, sandbox := range f.sandboxes {
		if sandboxName != nil && sandbox == *sandboxName {
			result[id] = sandbox
		}
		if sandboxNameContains != nil && strings.Contains(strings.ToLower(sandbox), strings.ToLower(*sandboxNameContains)) {
			result[id] = sandbox
		}
	}
	return result, nil
}

func (*threadReadHistoryFake) Observations(context.Context, uuid.UUID, *uint64, *uint64, int) (historydata.ObservationPage, error) {
	return historydata.ObservationPage{}, errors.New("unexpected observations call")
}

func (*threadReadHistoryFake) ObservationEntry(context.Context, uuid.UUID, uint64, uint64) (*historydata.ObservationEntry, error) {
	return nil, errors.New("unexpected observation entry call")
}

func (*threadReadHistoryFake) Evidence(context.Context, uuid.UUID, string, string, string, uint64, int) (historydata.EvidencePage, error) {
	return historydata.EvidencePage{}, errors.New("unexpected evidence call")
}

func (*threadReadHistoryFake) NativeFrames(context.Context, uuid.UUID, string, string, string, uint64, uint64, int) (historydata.NativeFramePage, error) {
	return historydata.NativeFramePage{}, errors.New("unexpected native frame call")
}

func TestListThreadViewsFiltersHistoryAndContinuesFromLastReturnedThread(t *testing.T) {
	ids := []uuid.UUID{
		uuid.MustParse("00000000-0000-0000-0000-000000000004"),
		uuid.MustParse("00000000-0000-0000-0000-000000000003"),
		uuid.MustParse("00000000-0000-0000-0000-000000000002"),
		uuid.MustParse("00000000-0000-0000-0000-000000000001"),
	}
	base := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	name := "Router and auth"
	rows := []threads.Thread{
		{ID: ids[0], CreatedAt: base, Name: &name},
		{ID: ids[1], CreatedAt: base.Add(-time.Minute)},
		{ID: ids[2], CreatedAt: base.Add(-2 * time.Minute), Name: &name}, // History summary absent.
		{ID: ids[3], CreatedAt: base.Add(-3 * time.Minute), Name: &name},
	}
	summaries := map[uuid.UUID]historydata.FoldSummary{
		ids[0]: {Present: true, Sandbox: "team-alpha"},
		ids[1]: {Present: true, Sandbox: "team-beta"},
		ids[3]: {Present: true, Sandbox: "team-alpha"},
	}
	store := &threadReadStoreFake{rows: rows}
	history := &threadReadHistoryFake{
		summaries: summaries,
		sandboxes: map[uuid.UUID]string{ids[0]: "team-alpha", ids[1]: "team-beta", ids[3]: "team-alpha"},
	}
	listing := threadListing{Search: stringPointer("alpha"), IncludeArchived: false}
	first, err := listThreadViews(context.Background(), store, history, listing, 1, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(first.Threads) != 1 || first.Threads[0].ID != ids[0] || first.NextPageToken == nil {
		t.Fatalf("first page = %+v, want newest matching Thread and a continuation", first)
	}
	second, err := listThreadViews(context.Background(), store, history, listing, 1, first.NextPageToken)
	if err != nil {
		t.Fatal(err)
	}
	if len(second.Threads) != 1 || second.Threads[0].ID != ids[3] || second.NextPageToken != nil {
		t.Fatalf("second page = %+v, want older matching Thread and no continuation", second)
	}
	if _, err := decodeThreadPageToken(first.NextPageToken, threadListing{Search: stringPointer("beta")}); !errors.Is(err, errMismatchedThreadListing) {
		t.Fatalf("page token filter mismatch = %v, want errMismatchedThreadListing", err)
	}

	exact := threadListing{Sandbox: stringPointer("team-beta"), IncludeArchived: false}
	filtered, err := listThreadViews(context.Background(), store, history, exact, 10, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(filtered.Threads) != 1 || filtered.Threads[0].ID != ids[1] {
		t.Fatalf("exact Sandbox filter = %+v, want only %s", filtered.Threads, ids[1])
	}
}

func TestMakeThreadViewPreservesFeedStatusEnum(t *testing.T) {
	thread := threads.Thread{ID: uuid.New(), CreatedAt: time.Now().UTC()}
	attachment := &runnerpb.Attached{HarnessState: runnerpb.HarnessState_HARNESS_STATE_STOPPED}
	for _, test := range []struct {
		status historypb.FeedStatus
		want   string
	}{
		{historypb.FeedStatus_FEED_STATUS_ACTIVE, "active"},
		{historypb.FeedStatus_FEED_STATUS_ENDED, "ended"},
		{historypb.FeedStatus_FEED_STATUS_FAILED, "failed"},
	} {
		t.Run(test.want, func(t *testing.T) {
			view, err := makeThreadView(thread, historydata.FoldSummary{
				Present: true, Sandbox: "sandbox",
				Feed: &historydata.FeedSnapshot{Status: test.status, Attached: attachment},
			})
			if err != nil {
				t.Fatal(err)
			}
			if view.FeedStatus == nil || *view.FeedStatus != test.want {
				t.Fatalf("feed_status = %v, want %q", view.FeedStatus, test.want)
			}
		})
	}
	view, err := makeThreadView(thread, historydata.FoldSummary{Present: true, Sandbox: "sandbox"})
	if err != nil {
		t.Fatal(err)
	}
	if view.FeedStatus != nil || view.HarnessState != runnerpb.HarnessState_HARNESS_STATE_UNSPECIFIED.String() {
		t.Fatalf("unattached view = %+v, want nil feed and unspecified harness", view)
	}
}

func TestThreadRenameBodyRequiresOneKnownField(t *testing.T) {
	for _, test := range []struct {
		name    string
		body    string
		wantErr bool
	}{
		{name: "missing", body: `{}`, wantErr: true},
		{name: "null clears", body: `{"name":null}`},
		{name: "string", body: `{"name":"  title  "}`},
		{name: "extra field", body: `{"name":"title","extra":true}`, wantErr: true},
		{name: "wrong type", body: `{"name":4}`, wantErr: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var input threadRenameBody
			err := json.Unmarshal([]byte(test.body), &input)
			if (err != nil) != test.wantErr {
				t.Fatalf("Unmarshal error = %v, wantErr %v", err, test.wantErr)
			}
			if err == nil && !input.Present {
				t.Fatal("name field was not marked present")
			}
		})
	}
}

func TestThreadReadRouteRegistrationPublishesOpenAPIPaths(t *testing.T) {
	server := NewHTTPServer(nil)
	RegisterThreadReadRoutes(server, nil, nil, nil, false, nil, nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d; body: %s", recorder.Code, recorder.Body.String())
	}
	for _, path := range []string{
		"/threads/form", "/threads", "/threads/{thread_id}", "/threads/{thread_id}/archive",
		"/threads/{thread_id}/unarchive", "/threads/{thread_id}/auto-title",
		"/threads/{thread_id}/observations", "/threads/{thread_id}/observations/{cursor}",
		"/threads/{thread_id}/evidence", "/threads/{thread_id}/evidence/{observation_cursor}/frames",
	} {
		if !strings.Contains(recorder.Body.String(), `"`+path+`"`) {
			t.Errorf("OpenAPI does not describe %s", path)
		}
	}
	var document struct {
		Paths map[string]map[string]struct {
			Parameters []struct {
				Name     string `json:"name"`
				In       string `json:"in"`
				Required bool   `json:"required"`
			} `json:"parameters"`
		} `json:"paths"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &document); err != nil {
		t.Fatalf("decode OpenAPI document: %v", err)
	}
	for _, path := range []string{"/threads/{thread_id}/evidence", "/threads/{thread_id}/evidence/{observation_cursor}/frames"} {
		get, ok := document.Paths[path]["get"]
		if !ok {
			t.Fatalf("OpenAPI has no GET operation for %s", path)
		}
		parameters := make(map[string]bool, len(get.Parameters))
		for _, parameter := range get.Parameters {
			if parameter.In == "query" {
				parameters[parameter.Name] = parameter.Required
			}
		}
		for _, name := range []string{"projection_epoch", "entity_kind", "entity_id"} {
			if required, present := parameters[name]; !present || !required {
				t.Errorf("GET %s query parameter %q required = %t, present = %t; want required", path, name, required, present)
			}
		}
	}
}

func TestEvidenceCursorsMatchSignedDecimalBounds(t *testing.T) {
	for _, value := range []string{"0", "1", "9223372036854775807"} {
		parsed, err := parseHistoryCursor(stringPointer(value))
		if err != nil || parsed == nil {
			t.Errorf("parseHistoryCursor(%q) = %v, %v", value, parsed, err)
		}
	}
	for _, value := range []string{"", "-1", "1.2", "9223372036854775808"} {
		if parsed, err := parseHistoryCursor(stringPointer(value)); err == nil {
			t.Errorf("parseHistoryCursor(%q) = %v, want error", value, parsed)
		}
	}
}

func TestEvidenceErrorsMapToPythonStatusesAndDetails(t *testing.T) {
	for _, test := range []struct {
		name       string
		err        error
		wantStatus int
		wantDetail string
	}{
		{name: "changed projection epoch", err: historydata.ErrScopeChanged, wantStatus: http.StatusGone, wantDetail: "the thread fold projection epoch has changed"},
		{name: "missing entity or observation", err: historydata.ErrEvidenceNotFound, wantStatus: http.StatusNotFound, wantDetail: "no selected thread entity"},
	} {
		t.Run(test.name, func(t *testing.T) {
			mapped := threadEvidenceHTTPError(test.err)
			var statusError interface{ GetStatus() int }
			if !errors.As(mapped, &statusError) {
				t.Fatalf("mapped error %T does not expose an HTTP status", mapped)
			}
			if statusError.GetStatus() != test.wantStatus || !strings.Contains(mapped.Error(), test.wantDetail) {
				t.Fatalf("mapped error = (%d, %q), want status %d and detail %q", statusError.GetStatus(), mapped.Error(), test.wantStatus, test.wantDetail)
			}
		})
	}
}

func TestEvidenceJSONUsesSnakeCaseCursorStringsAndNulls(t *testing.T) {
	evidence, err := json.Marshal(historydata.EvidencePage{
		Observations:    []historydata.EvidenceObservation{{ObservationCursor: "9007199254740993", HasNative: true}},
		NextAfterCursor: stringPointer("9007199254740993"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(evidence), `{"observations":[{"observation_cursor":"9007199254740993","has_native":true}],"next_after_cursor":"9007199254740993"}`; got != want {
		t.Fatalf("evidence JSON = %s, want %s", got, want)
	}
	frames, err := json.Marshal(historydata.NativeFramePage{
		Frames: []historydata.NativeFrame{{SourceSequence: "12", Availability: "unavailable", Entry: nil}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(frames), `{"frames":[{"source_sequence":"12","availability":"unavailable","entry":null}],"next_after_sequence":null}`; got != want {
		t.Fatalf("native frame JSON = %s, want %s", got, want)
	}
}

func TestThreadSandboxReadinessRequiresRunningReadyPod(t *testing.T) {
	status, err := structpb.NewStruct(map[string]any{"conditions": []any{
		map[string]any{"type": "Ready", "status": "True"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !threadSandboxReady(&sandboxespb.Sandbox{OperatingMode: stringPointer("Running"), Status: status}) {
		t.Fatal("running Sandbox with Ready=True was rejected")
	}
	if threadSandboxReady(&sandboxespb.Sandbox{OperatingMode: stringPointer("Suspended"), Status: status}) {
		t.Fatal("suspended Sandbox was accepted")
	}
}

func TestEnsureThreadArchivableRejectsBusyRunnerButAllowsIdleRunner(t *testing.T) {
	status, err := structpb.NewStruct(map[string]any{"conditions": []any{
		map[string]any{"type": "Ready", "status": "True"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	thread := threads.Thread{ID: id}
	sandbox := &sandboxespb.Sandbox{
		OperatingMode: stringPointer("Running"), Status: status,
		Pod: &sandboxespb.SandboxPod{Name: "runner-pod"},
	}
	inventory := &threadReadInventoryFake{sandbox: sandbox}
	runner := &threadReadRunnerFake{status: &runnerpb.RunnerStatus{Sessions: []*runnerpb.SessionStatus{{SessionId: id.String(), Busy: true}}}}
	if err := ensureThreadArchivable(context.Background(), thread, historydata.FoldSummary{Sandbox: "sandbox"}, inventory, runner); !errors.Is(err, errThreadBusy) {
		t.Fatalf("busy runner archive check = %v, want errThreadBusy", err)
	}
	runner.status.Sessions[0].Busy = false
	if err := ensureThreadArchivable(context.Background(), thread, historydata.FoldSummary{Sandbox: "sandbox"}, inventory, runner); err != nil {
		t.Fatalf("idle runner archive check = %v, want nil", err)
	}
	sandbox.OperatingMode = stringPointer("Suspended")
	if err := ensureThreadArchivable(context.Background(), thread, historydata.FoldSummary{Sandbox: "sandbox"}, inventory, runner); !errors.Is(err, errThreadRunnerUnready) {
		t.Fatalf("unready Sandbox archive check = %v, want errThreadRunnerUnready", err)
	}
}
