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
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
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

func (f *threadReadStoreFake) ArchivedCount(context.Context) (int, error) {
	count := 0
	for _, row := range f.rows {
		if row.Archived {
			count++
		}
	}
	return count, nil
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
	summaries   map[uuid.UUID]historydata.FoldSummary
	sandboxes   map[uuid.UUID]string
	sandboxUIDs map[uuid.UUID]string
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

func (f *threadReadHistoryFake) FindSessions(_ context.Context, sandboxName, sandboxNameContains, sandboxUID *string) (map[uuid.UUID]string, error) {
	result := make(map[uuid.UUID]string)
	for id, sandbox := range f.sandboxes {
		if sandboxName != nil && sandbox == *sandboxName {
			result[id] = sandbox
		}
		if sandboxNameContains != nil && strings.Contains(strings.ToLower(sandbox), strings.ToLower(*sandboxNameContains)) {
			result[id] = sandbox
		}
		if sandboxUID != nil && f.sandboxUIDs[id] == *sandboxUID {
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

func TestListThreadViewsFiltersBySandboxIncarnationAndBindsPageToken(t *testing.T) {
	ids := []uuid.UUID{
		uuid.MustParse("00000000-0000-0000-0000-000000000014"),
		uuid.MustParse("00000000-0000-0000-0000-000000000013"),
		uuid.MustParse("00000000-0000-0000-0000-000000000012"),
	}
	uid := "11111111-1111-4111-8111-111111111111"
	replacementUID := "22222222-2222-4222-8222-222222222222"
	base := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	store := &threadReadStoreFake{rows: []threads.Thread{
		{ID: ids[0], CreatedAt: base, Archived: true},
		{ID: ids[1], CreatedAt: base.Add(-time.Minute), Archived: true},
		{ID: ids[2], CreatedAt: base.Add(-2 * time.Minute), Archived: true},
	}}
	history := &threadReadHistoryFake{
		summaries: map[uuid.UUID]historydata.FoldSummary{
			ids[0]: {Present: true, Sandbox: "reused-name", SandboxUID: uid},
			ids[1]: {Present: true, Sandbox: "reused-name", SandboxUID: replacementUID},
			ids[2]: {Present: true, Sandbox: "other-name", SandboxUID: uid},
		},
		sandboxes:   map[uuid.UUID]string{ids[0]: "reused-name", ids[1]: "reused-name", ids[2]: "other-name"},
		sandboxUIDs: map[uuid.UUID]string{ids[0]: uid, ids[1]: replacementUID, ids[2]: uid},
	}
	listing := threadListing{SandboxUID: stringPointer(uid), IncludeArchived: true}
	first, err := listThreadViews(context.Background(), store, history, listing, 1, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(first.Threads) != 1 || first.Threads[0].ID != ids[0] || first.Threads[0].SandboxUID == nil || first.Threads[0].SandboxUID.String() != uid || first.NextPageToken == nil {
		t.Fatalf("first page = %+v, want archived Thread %s with UID %s and a continuation", first, ids[0], uid)
	}
	second, err := listThreadViews(context.Background(), store, history, listing, 1, first.NextPageToken)
	if err != nil {
		t.Fatal(err)
	}
	if len(second.Threads) != 1 || second.Threads[0].ID != ids[2] || second.Threads[0].SandboxUID == nil || second.Threads[0].SandboxUID.String() != uid || second.NextPageToken != nil {
		t.Fatalf("second page = %+v, want older Thread %s with UID %s and no continuation", second, ids[2], uid)
	}
	otherListing := threadListing{SandboxUID: stringPointer(replacementUID), IncludeArchived: true}
	if _, err := decodeThreadPageToken(first.NextPageToken, otherListing); !errors.Is(err, errMismatchedThreadListing) {
		t.Fatalf("page token UID mismatch = %v, want errMismatchedThreadListing", err)
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

func TestMakeThreadViewReturnsSandboxUID(t *testing.T) {
	uid := "11111111-1111-4111-8111-111111111111"
	view, err := makeThreadView(threads.Thread{ID: uuid.New()}, historydata.FoldSummary{
		Present: true, Sandbox: "sandbox", SandboxUID: uid,
	})
	if err != nil {
		t.Fatal(err)
	}
	if view.SandboxUID == nil || view.SandboxUID.String() != uid {
		t.Fatalf("sandbox_uid = %v, want %s", view.SandboxUID, uid)
	}
	body, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatal(err)
	}
	if got, ok := decoded["sandbox_uid"].(string); !ok || got != uid {
		t.Fatalf("JSON sandbox_uid = %v, want %q", decoded["sandbox_uid"], uid)
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
	RegisterThreadReadRoutes(server, nil, nil, nil, nil, nil, nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d; body: %s", recorder.Code, recorder.Body.String())
	}
	for _, path := range []string{"/threads/{thread_id}", "/threads/{thread_id}/archive", "/threads/{thread_id}/unarchive"} {
		if !strings.Contains(recorder.Body.String(), `"`+path+`"`) {
			t.Errorf("OpenAPI does not describe %s", path)
		}
	}
	for _, path := range []string{"/threads", "/threads/{thread_id}/observations", "/threads/{thread_id}/observations/{cursor}", "/threads/{thread_id}/evidence"} {
		if strings.Contains(recorder.Body.String(), `"`+path+`"`) {
			t.Errorf("OpenAPI still documents app-owned History wrapper %s", path)
		}
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
	if threadSandboxReady(&sandboxespb.Sandbox{Status: status}) {
		t.Fatal("Ready condition without a reported mode was accepted")
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

func TestThreadViewReadsHistoryIdentityActivityAndAttachedSpecification(t *testing.T) {
	id := uuid.New()
	name := "operator name"
	at := time.Date(2026, 10, 29, 1, 2, 3, 0, time.UTC)
	row := threads.Thread{ID: id, Name: &name, Archived: true, CreatedAt: at.Add(-time.Hour)}
	for _, turn := range []eventpb.TurnStatus{
		eventpb.TurnStatus_TURN_STATUS_COMPLETED,
		eventpb.TurnStatus_TURN_STATUS_FAILED,
		eventpb.TurnStatus_TURN_STATUS_INTERRUPTED,
	} {
		t.Run(turn.String(), func(t *testing.T) {
			status := int32(turn)
			view, err := makeThreadView(row, historydata.FoldSummary{
				Present: true, Sandbox: "history-owned", FoldedThrough: 7, CommittedCursor: 9,
				LastEventAt: &at, LastModelActivityAt: &at, IdleSince: &at, LastTurnStatus: &status,
				Feed: &historydata.FeedSnapshot{
					Status: historypb.FeedStatus_FEED_STATUS_ENDED,
					Attached: &runnerpb.Attached{
						HarnessState: runnerpb.HarnessState_HARNESS_STATE_STOPPED,
						Spec:         &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CODEX, Model: "model", Cwd: "/workspace", ReasoningEffort: "high"},
					},
				},
			})
			if err != nil {
				t.Fatal(err)
			}
			if view.ID != id || view.Sandbox != "history-owned" || view.Name != &name || !view.Archived || view.CreatedAt != row.CreatedAt ||
				view.LastCursor != 7 || view.LastEventAt != &at || view.LastModelActivityAt != &at || view.IdleSince != &at ||
				view.LastTurnStatus == nil || *view.LastTurnStatus != turn.String() || view.FeedStatus == nil || *view.FeedStatus != "ended" ||
				view.Harness == nil || *view.Harness != "HARNESS_CODEX" || view.Model == nil || *view.Model != "model" ||
				view.CWD == nil || *view.CWD != "/workspace" || view.ReasoningEffort == nil || *view.ReasoningEffort != "high" {
				t.Fatalf("Thread view lost History or operator fields: %+v", view)
			}
		})
	}
	view, err := makeThreadView(row, historydata.FoldSummary{Present: true, Sandbox: "unfolded"})
	if err != nil || view.LastCursor != 0 || view.FeedStatus != nil || view.Harness != nil || view.Model != nil || view.CWD != nil || view.LastTurnStatus != nil {
		t.Fatalf("unfolded Thread view = %+v, %v", view, err)
	}
	if _, err := makeThreadView(row, historydata.FoldSummary{Present: true}); err == nil {
		t.Fatal("committed History without Sandbox identity was accepted")
	}
	if _, err := getThreadView(t.Context(), &threadReadStoreFake{rows: []threads.Thread{row}}, &threadReadHistoryFake{}, id); err == nil {
		t.Fatal("pending History identity was exposed")
	}
}
