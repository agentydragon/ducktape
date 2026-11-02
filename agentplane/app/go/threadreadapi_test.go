package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/environmentpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestListThreadMetadataScopesPagesToExactEnvironmentUID(t *testing.T) {
	first, replacement, last := uuid.New(), uuid.New(), uuid.New()
	now := time.Now()
	store := &threadReadStoreFake{rows: []threads.Thread{
		{ID: first, CreatedAt: now},
		{ID: replacement, CreatedAt: now.Add(-time.Second)},
		{ID: last, CreatedAt: now.Add(-2 * time.Second), Archived: true},
	}}
	history := &threadReadHistoryFake{
		summaries: map[uuid.UUID]historydata.FoldSummary{
			first:       {Present: true, Sandbox: "worker", SandboxUID: "old"},
			replacement: {Present: true, Sandbox: "worker", SandboxUID: "new"},
			last:        {Present: true, Sandbox: "worker", SandboxUID: "old"},
		},
		sandboxUIDs: map[uuid.UUID]string{first: "old", replacement: "new", last: "old"},
		sandboxes:   map[uuid.UUID]string{first: "worker", replacement: "worker", last: "worker"},
	}
	service := &appService{threadReader: store, threadHistory: history}
	page, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: "old", Limit: 1, IncludeArchived: true})
	if err != nil || len(page.GetThreads()) != 1 || page.GetThreads()[0].GetThreadId() != first.String() || page.GetNextPageToken() == "" || page.GetArchivedCount() != 1 {
		t.Fatalf("first page = %v, %v", page, err)
	}
	next, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: "old", Limit: 1, IncludeArchived: true, PageToken: page.GetNextPageToken()})
	if err != nil || len(next.GetThreads()) != 1 || next.GetThreads()[0].GetThreadId() != last.String() || next.GetNextPageToken() != "" {
		t.Fatalf("next page = %v, %v", next, err)
	}
	_, err = service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: "new", Limit: 1, IncludeArchived: true, PageToken: page.GetNextPageToken()})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("continuation for another UID = %v, want InvalidArgument", err)
	}
}

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
	err         error
}

func TestListThreadMetadataScopesPagesAndArchiveCountToSandboxUID(t *testing.T) {
	const oldUID = "11111111-1111-4111-8111-111111111111"
	const currentUID = "22222222-2222-4222-8222-222222222222"
	ids := []uuid.UUID{
		uuid.MustParse("00000000-0000-4000-8000-000000000001"),
		uuid.MustParse("00000000-0000-4000-8000-000000000002"),
		uuid.MustParse("00000000-0000-4000-8000-000000000003"),
		uuid.MustParse("00000000-0000-4000-8000-000000000004"),
		uuid.MustParse("00000000-0000-4000-8000-000000000005"),
	}
	store := &threadReadStoreFake{rows: []threads.Thread{
		{ID: ids[0], CreatedAt: time.Unix(4, 0), Archived: true},
		{ID: ids[1], CreatedAt: time.Unix(3, 0)},
		{ID: ids[2], CreatedAt: time.Unix(2, 0)},
		{ID: ids[3], CreatedAt: time.Unix(1, 0), Archived: true},
		{ID: ids[4], CreatedAt: time.Unix(0, 0), Archived: true},
	}}
	history := &threadReadHistoryFake{
		summaries: map[uuid.UUID]historydata.FoldSummary{
			ids[0]: {Present: true, Sandbox: "reused", SandboxUID: oldUID},
			ids[1]: {Present: true, Sandbox: "reused", SandboxUID: currentUID},
			ids[2]: {Present: true, Sandbox: "reused", SandboxUID: currentUID},
			ids[3]: {Present: true, Sandbox: "reused", SandboxUID: currentUID},
			ids[4]: {Present: false, Sandbox: "reused", SandboxUID: currentUID},
		},
		sandboxes:   map[uuid.UUID]string{ids[0]: "reused", ids[1]: "reused", ids[2]: "reused", ids[3]: "reused", ids[4]: "reused"},
		sandboxUIDs: map[uuid.UUID]string{ids[0]: oldUID, ids[1]: currentUID, ids[2]: currentUID, ids[3]: currentUID, ids[4]: currentUID},
	}
	service := &appService{threadReader: store, threadHistory: history}
	first, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: currentUID, Limit: 1, IncludeArchived: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(first.Threads) != 1 || first.Threads[0].ThreadId != ids[1].String() || first.NextPageToken == "" || first.ArchivedCount != 1 {
		t.Fatalf("first current-incarnation page = %+v", first)
	}
	second, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: currentUID, Limit: 1, IncludeArchived: true, PageToken: first.NextPageToken})
	if err != nil {
		t.Fatal(err)
	}
	if len(second.Threads) != 1 || second.Threads[0].ThreadId != ids[2].String() || second.ArchivedCount != 1 {
		t.Fatalf("second current-incarnation page = %+v", second)
	}
	_, err = service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: oldUID, Limit: 1, IncludeArchived: true, PageToken: first.NextPageToken})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("cross-incarnation page token = %v", err)
	}
	defaultPage, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: currentUID, Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if defaultPage.ArchivedCount != 1 || defaultPage.Threads[0].ThreadId != ids[1].String() {
		t.Fatalf("default current-incarnation page = %+v", defaultPage)
	}
	history.err = errors.New("History unavailable")
	_, err = service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: currentUID})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("History failure = %v", err)
	}
}

type threadReadAcceptedTokenReviewer struct{}

func (threadReadAcceptedTokenReviewer) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}, nil
}

type threadMetadataChangesFake struct {
	updates      chan struct{}
	unsubscribed chan struct{}
}

func (f *threadMetadataChangesFake) Subscribe(...string) (<-chan struct{}, func(), error) {
	return f.updates, func() { close(f.unsubscribed) }, nil
}

func TestThreadMetadataWatchStreamsUpdatesAndCloses(t *testing.T) {
	for _, stop := range []string{"cancel", "drain"} {
		t.Run(stop, func(t *testing.T) {
			authentication, err := auth.New(auth.Config{
				TokenReviews: threadReadAcceptedTokenReviewer{}, TokenAudience: "agentplane",
				TokenSubjects: []string{"system:serviceaccount:tools:runner"},
			})
			if err != nil {
				t.Fatal(err)
			}
			changes := &threadMetadataChangesFake{updates: make(chan struct{}, 1), unsubscribed: make(chan struct{})}
			var connected atomic.Bool
			connected.Store(true)
			server := NewHTTPServer(authentication)
			if err := registerThreadMetadataGateway(server, &appService{threadReader: &threadReadStoreFake{}, updates: changes, updatesConnected: connected.Load, drainDone: server.DrainDone()}); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(server.Close)
			httpServer := httptest.NewServer(server.Handler(authentication))
			t.Cleanup(httpServer.Close)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			request, err := http.NewRequestWithContext(ctx, http.MethodGet, httpServer.URL+"/upstream/app/v1/threads:watch", nil)
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Authorization", "Bearer accepted")
			request.Header.Set("Accept", "application/x-ndjson")
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode != http.StatusOK {
				t.Fatalf("watch status = %d", response.StatusCode)
			}
			lines := bufio.NewReader(response.Body)
			readFrame := func(wantConnected bool) {
				t.Helper()
				line, err := lines.ReadBytes('\n')
				if err != nil {
					t.Fatal(err)
				}
				var frame struct {
					Result struct {
						Changed          json.RawMessage `json:"changed"`
						UpdatesConnected bool            `json:"updatesConnected"`
					} `json:"result"`
				}
				if err := json.Unmarshal(line, &frame); err != nil || string(frame.Result.Changed) != "{}" || frame.Result.UpdatesConnected != wantConnected {
					t.Fatalf("frame %s: %v", line, err)
				}
			}
			readFrame(true)
			connected.Store(false)
			changes.updates <- struct{}{}
			readFrame(false)
			if stop == "cancel" {
				cancel()
			} else {
				server.BeginDrain()
				if _, err := lines.ReadByte(); !errors.Is(err, io.EOF) {
					t.Fatalf("drained watch returned %v, want EOF", err)
				}
			}
			select {
			case <-changes.unsubscribed:
			case <-time.After(5 * time.Second):
				t.Fatal("watch did not unsubscribe")
			}
		})
	}
}

type threadReadInventoryFake struct {
	sandbox   *environmentpb.EnvironmentDescriptor
	err       error
	exactName string
	exactUID  string
}

func (f *threadReadInventoryFake) Get(context.Context, string) (*environmentpb.EnvironmentDescriptor, error) {
	return f.sandbox, f.err
}

func (f *threadReadInventoryFake) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	target := request.GetTarget()
	name, uid := target.GetName(), target.GetUid()
	f.exactName, f.exactUID = name, uid
	if f.err != nil {
		return nil, f.err
	}
	if f.sandbox.GetTarget().GetName() != name || f.sandbox.GetTarget().GetUid() != uid {
		return nil, errors.New("exact target UID mismatch")
	}
	return f.sandbox, nil
}

type threadReadEnvironmentRuntimeFake struct {
	status *sessionpb.EnvironmentRuntimeStatus
	err    error
	target *environmentpb.TargetHandle
}

func (f *threadReadEnvironmentRuntimeFake) EnvironmentRuntimeStatus(_ context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	f.target = sandbox.GetTarget()
	return f.status, f.err
}

func (f *threadReadHistoryFake) GetSummary(_ context.Context, id uuid.UUID) (historydata.FoldSummary, error) {
	return f.summaries[id], nil
}

func (f *threadReadHistoryFake) BatchGetSummaries(_ context.Context, ids []uuid.UUID) (map[uuid.UUID]historydata.FoldSummary, error) {
	if f.err != nil {
		return nil, f.err
	}
	result := make(map[uuid.UUID]historydata.FoldSummary, len(ids))
	for _, id := range ids {
		result[id] = f.summaries[id]
	}
	return result, nil
}

func (f *threadReadHistoryFake) FindSessions(_ context.Context, sandboxName, sandboxNameContains, sandboxUID *string) (map[uuid.UUID]string, error) {
	if f.err != nil {
		return nil, f.err
	}
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
	if len(first.Threads) != 1 || first.Threads[0].ID != ids[0] || first.Threads[0].SandboxUID == nil || *first.Threads[0].SandboxUID != uid || first.NextPageToken == nil {
		t.Fatalf("first page = %+v, want archived Thread %s with UID %s and a continuation", first, ids[0], uid)
	}
	second, err := listThreadViews(context.Background(), store, history, listing, 1, first.NextPageToken)
	if err != nil {
		t.Fatal(err)
	}
	if len(second.Threads) != 1 || second.Threads[0].ID != ids[2] || second.Threads[0].SandboxUID == nil || *second.Threads[0].SandboxUID != uid || second.NextPageToken != nil {
		t.Fatalf("second page = %+v, want older Thread %s with UID %s and no continuation", second, ids[2], uid)
	}
	otherListing := threadListing{SandboxUID: stringPointer(replacementUID), IncludeArchived: true}
	if _, err := decodeThreadPageToken(first.NextPageToken, otherListing); !errors.Is(err, errMismatchedThreadListing) {
		t.Fatalf("page token UID mismatch = %v, want errMismatchedThreadListing", err)
	}
}

func TestMakeThreadViewPreservesFeedStatusEnum(t *testing.T) {
	thread := threads.Thread{ID: uuid.New(), CreatedAt: time.Now().UTC()}
	attachment := &sessionpb.Attached{HarnessState: sessionpb.HarnessState_HARNESS_STATE_STOPPED}
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
	if view.FeedStatus != nil || view.HarnessState != sessionpb.HarnessState_HARNESS_STATE_UNSPECIFIED.String() {
		t.Fatalf("unattached view = %+v, want nil feed and unspecified harness", view)
	}
}

func TestMakeThreadViewReturnsSandboxUID(t *testing.T) {
	uid := "opaque-sandbox-uid"
	view, err := makeThreadView(threads.Thread{ID: uuid.New()}, historydata.FoldSummary{
		Present: true, Sandbox: "sandbox", SandboxUID: uid,
	})
	if err != nil {
		t.Fatal(err)
	}
	if view.SandboxUID == nil || *view.SandboxUID != uid {
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
	if err := RegisterThreadReadRoutes(server, "tools", nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
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
	ready := &environmentpb.EnvironmentDescriptor{
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
	}
	if !threadSandboxReady(ready) {
		t.Fatal("running Sandbox with Ready=True was rejected")
	}
	if threadSandboxReady(&environmentpb.EnvironmentDescriptor{ObservedState: &environmentpb.EnvironmentObservedState{Readiness: ready.GetObservedState().GetReadiness()}}) {
		t.Fatal("Ready condition without a reported mode was accepted")
	}
	if threadSandboxReady(&environmentpb.EnvironmentDescriptor{
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED,
			Readiness:     ready.GetObservedState().GetReadiness(),
		},
	}) {
		t.Fatal("suspended Sandbox was accepted")
	}
}

func TestEnsureThreadArchivableRejectsBusyEnvironmentRuntime(t *testing.T) {
	id := uuid.New()
	thread := threads.Thread{ID: id}
	sandbox := &environmentpb.EnvironmentDescriptor{
		Target: &environmentpb.TargetHandle{Namespace: "team", Name: "sandbox", Uid: "sandbox-uid"},
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
		KubernetesDiagnostics: &environmentpb.KubernetesDiagnostics{Pod: &environmentpb.SandboxPod{Name: "environment-runtime-pod"}},
	}
	inventory := &threadReadInventoryFake{sandbox: sandbox}
	environmentRuntime := &threadReadEnvironmentRuntimeFake{status: &sessionpb.EnvironmentRuntimeStatus{Sessions: []*sessionpb.SessionStatus{{SessionId: id.String(), Busy: true}}}}
	if err := ensureThreadArchivable(context.Background(), "tools", thread, historydata.FoldSummary{Sandbox: "sandbox", SandboxUID: "sandbox-uid"}, inventory, environmentRuntime); !errors.Is(err, errThreadBusy) {
		t.Fatalf("busy Environment Runtime archive check = %v, want errThreadBusy", err)
	}
	if inventory.exactName != "sandbox" || inventory.exactUID != "sandbox-uid" {
		t.Fatalf("archive lookup identity = %q/%q, want exact Sandbox UID", inventory.exactName, inventory.exactUID)
	}
	if environmentRuntime.target.GetUid() != "sandbox-uid" {
		t.Fatalf("runtime status target = %v, want exact Sandbox UID", environmentRuntime.target)
	}
	environmentRuntime.status.Sessions[0].Busy = false
	if err := ensureThreadArchivable(context.Background(), "tools", thread, historydata.FoldSummary{Sandbox: "sandbox", SandboxUID: "sandbox-uid"}, inventory, environmentRuntime); err != nil {
		t.Fatalf("idle Environment Runtime archive check = %v, want nil", err)
	}
	sandbox.ObservedState.OperatingMode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
	if err := ensureThreadArchivable(context.Background(), "tools", thread, historydata.FoldSummary{Sandbox: "sandbox", SandboxUID: "sandbox-uid"}, inventory, environmentRuntime); !errors.Is(err, errThreadEnvironmentRuntimeUnready) {
		t.Fatalf("unready Environment Runtime archive check = %v, want errThreadEnvironmentRuntimeUnready", err)
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
					Attached: &sessionpb.Attached{
						HarnessState: sessionpb.HarnessState_HARNESS_STATE_STOPPED,
						Spec:         &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CODEX, Model: "model", Cwd: "/workspace", ReasoningEffort: "high"},
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
}

func TestRenameThreadReturnsNotFoundWhenHistorySessionIsMissing(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: threadReadAcceptedTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:tools:runner"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	t.Cleanup(server.Close)
	id := uuid.New()
	store := &threadReadStoreFake{rows: []threads.Thread{{ID: id}}}
	if err := RegisterThreadReadRoutes(server, "tools", store, &threadReadHistoryFake{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPatch, "/threads/"+id.String(), strings.NewReader(`{"name":"renamed"}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("PATCH /threads/{thread_id} with no History session = %d %s, want 404", response.Code, response.Body.String())
	}
	if store.rows[0].Name != nil {
		t.Fatalf("Thread was renamed without a History session: %q", *store.rows[0].Name)
	}
}
