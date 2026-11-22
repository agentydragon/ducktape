package server

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	threadhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/threads"
	"github.com/agentydragon/ducktape/agentplane/app/internal/server/testauth"
	"github.com/agentydragon/ducktape/agentplane/app/internal/threads"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type threadReadStoreFake struct {
	threadhttp.NoticeStore
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
	row := threads.Thread{ID: id, CreatedAt: time.Now(), Name: name}
	f.rows = append(f.rows, row)
	return row, nil
}

func (f *threadReadStoreFake) Archive(_ context.Context, id uuid.UUID) (threads.Thread, error) {
	for index, row := range f.rows {
		if row.ID == id {
			f.rows[index].Archived = true
			return f.rows[index], nil
		}
	}
	row := threads.Thread{ID: id, CreatedAt: time.Now(), Archived: true}
	f.rows = append(f.rows, row)
	return row, nil
}

func (f *threadReadStoreFake) Unarchive(_ context.Context, id uuid.UUID) (threads.Thread, error) {
	for index, row := range f.rows {
		if row.ID == id {
			f.rows[index].Archived = false
			return f.rows[index], nil
		}
	}
	row := threads.Thread{ID: id, CreatedAt: time.Now()}
	f.rows = append(f.rows, row)
	return row, nil
}

type threadReadDispatcherFake struct {
	session     *dispatcherpb.Session
	err         error
	calls       int
	requestedID string
}

func (f *threadReadDispatcherFake) GetSession(_ context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	f.calls++
	f.requestedID = request.GetSessionId()
	return &dispatcherpb.GetSessionResponse{Session: f.session}, f.err
}

func threadReadIdleSession(id uuid.UUID) *dispatcherpb.Session {
	return &dispatcherpb.Session{SessionId: id.String(), Status: &dispatcherpb.SessionStatus{Activity: &dispatcherpb.SessionActivity{
		Kind: &dispatcherpb.SessionActivity_Observed{Observed: &harnesshostpb.Session{SessionId: id.String(), Host: &harnesshostpb.Session_EnvironmentHosted{
			EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{Setup: sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED, Harness: &harnesshostpb.HarnessAttachment{State: harnesshostpb.HarnessAttachment_STATE_DETACHED}},
		}}},
	}}}
}

func TestListThreadMetadataPagesAppRowsWithoutHistory(t *testing.T) {
	ids := []uuid.UUID{uuid.New(), uuid.New(), uuid.New()}
	store := &threadReadStoreFake{rows: []threads.Thread{
		{ID: ids[0], CreatedAt: time.Unix(3, 0), Archived: true},
		{ID: ids[1], CreatedAt: time.Unix(2, 0), Name: proto.String("test-title")},
		{ID: ids[2], CreatedAt: time.Unix(1, 0)},
	}}
	handler := threadhttp.NewHandler(threadhttp.Dependencies{Store: store})
	token := ""
	for index, id := range ids {
		page, err := handler.ListThreadMetadata(t.Context(), &apppb.ListThreadMetadataRequest{Limit: 1, IncludeArchived: true, PageToken: token})
		if err != nil {
			t.Fatal(err)
		}
		if len(page.Threads) != 1 || page.Threads[0].GetThreadId() != id.String() || page.ArchivedCount != 1 {
			t.Fatalf("page %d = %v", index, page)
		}
		if (page.NextPageToken != "") != (index < len(ids)-1) {
			t.Fatalf("page %d token = %q", index, page.NextPageToken)
		}
		if index == 0 {
			_, err := handler.ListThreadMetadata(t.Context(), &apppb.ListThreadMetadataRequest{PageToken: page.NextPageToken})
			if status.Code(err) != codes.InvalidArgument {
				t.Fatalf("token with changed archive filter = %v", err)
			}
		}
		token = page.NextPageToken
	}
	page, err := handler.ListThreadMetadata(t.Context(), &apppb.ListThreadMetadataRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Threads) != 2 || page.Threads[0].GetThreadId() != ids[1].String() || page.Threads[0].GetName() != "test-title" || page.ArchivedCount != 1 {
		t.Fatalf("default page = %v", page)
	}
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
			fixture := testauth.New(t)
			authentication := fixture.Auth
			changes := &threadMetadataChangesFake{updates: make(chan struct{}, 1), unsubscribed: make(chan struct{})}
			var connected atomic.Bool
			connected.Store(true)
			server := newHTTPServer(authentication)
			threadMetadata := threadhttp.NewHandler(threadhttp.Dependencies{
				Store: &threadReadStoreFake{}, Updates: changes, UpdatesConnected: connected.Load, DrainDone: server.DrainDone(),
			})
			if err := threadMetadata.RegisterRoutes(server); err != nil {
				t.Fatal(err)
			}
			httpServer := httptest.NewServer(server.Handler(authentication))
			t.Cleanup(httpServer.Close)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			request, err := http.NewRequestWithContext(ctx, http.MethodGet, httpServer.URL+"/upstream/app/v1/threads:watch", nil)
			if err != nil {
				t.Fatal(err)
			}
			fixture.Authorize(request)
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

func TestThreadMutationsWithoutHistorySession(t *testing.T) {
	for _, operation := range []string{"rename", "archive", "unarchive"} {
		for _, existing := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/existing=%t", operation, existing), func(t *testing.T) {
				id := uuid.New()
				name := "test-title"
				createdAt := time.Unix(100, 0)
				store := &threadReadStoreFake{}
				if existing {
					store.rows = []threads.Thread{{ID: id, CreatedAt: createdAt, Name: &name, Archived: true}}
				}
				handler := threadhttp.NewHandler(threadhttp.Dependencies{Store: store, Dispatcher: &threadReadDispatcherFake{session: threadReadIdleSession(id)}})
				var err error
				switch operation {
				case "rename":
					_, err = handler.RenameThread(context.Background(), &apppb.RenameThreadRequest{ThreadId: id.String(), Name: proto.String("test-renamed")})
				case "archive":
					_, err = handler.ArchiveThread(context.Background(), &apppb.ThreadMetadataTarget{ThreadId: id.String()})
				case "unarchive":
					_, err = handler.UnarchiveThread(context.Background(), &apppb.ThreadMetadataTarget{ThreadId: id.String()})
				}
				if err != nil {
					t.Fatal(err)
				}
				metadata, err := handler.GetThreadMetadata(context.Background(), &apppb.ThreadMetadataTarget{ThreadId: id.String()})
				if err != nil {
					t.Fatal(err)
				}
				wantName := ""
				if operation == "rename" {
					wantName = "test-renamed"
				} else if existing {
					wantName = name
				}
				wantArchived := operation == "archive" || (existing && operation == "rename")
				if metadata.GetThreadId() != id.String() || metadata.GetName() != wantName || metadata.GetArchived() != wantArchived {
					t.Fatalf("metadata = %v, want name %q, archived %t", metadata, wantName, wantArchived)
				}
				if existing && !metadata.GetCreatedAt().AsTime().Equal(createdAt) {
					t.Fatalf("mutation changed creation time: %v", metadata.GetCreatedAt())
				}
			})
		}
	}
}

func TestThreadMutationGateway(t *testing.T) {
	id := uuid.New()
	store := &threadReadStoreFake{rows: []threads.Thread{{ID: id, CreatedAt: time.Now()}}}
	dispatcher := &threadReadDispatcherFake{session: threadReadIdleSession(id)}
	fixture := testauth.New(t)
	authentication := fixture.Auth
	server := newHTTPServer(authentication)
	threadMetadata := threadhttp.NewHandler(threadhttp.Dependencies{Store: store, Dispatcher: dispatcher})
	if err := threadMetadata.RegisterRoutes(server); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(authentication)
	path := "/upstream/app/v1/threads/" + id.String()
	call := func(method, path, body string, authorized bool) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if authorized {
			fixture.Authorize(req)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		return response
	}
	response := call(http.MethodPatch, path, `{"name":"  title  "}`, false)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized rename: %d %s", response.Code, response.Body.String())
	}
	response = call(http.MethodPatch, path, `{"name":"  title  "}`, true)
	if response.Code != http.StatusOK {
		t.Fatalf("rename: %d %s", response.Code, response.Body.String())
	}
	var metadata struct {
		ThreadID string `json:"threadId"`
		Name     string `json:"name"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &metadata); err != nil || metadata.ThreadID != id.String() || metadata.Name != "title" {
		t.Fatalf("renamed metadata = %s: %v", response.Body.String(), err)
	}
	response = call(http.MethodPatch, path, `{}`, true)
	if response.Code != http.StatusOK || store.rows[0].Name != nil {
		t.Fatalf("clear name: %d %s", response.Code, response.Body.String())
	}
	response = call(http.MethodPatch, path, `{"name":"`+strings.Repeat("x", 201)+`"}`, true)
	if response.Code != http.StatusUnprocessableEntity {
		t.Fatalf("overlong rename: %d %s", response.Code, response.Body.String())
	}
	// The browser's earlier idle observation cannot authorize a later archive.
	dispatcher.session.Status.Activity.GetObserved().ActiveTurnId = "test-active-turn"
	response = call(http.MethodPost, path+"/archive", `{}`, true)
	if response.Code != http.StatusConflict || store.rows[0].Archived || dispatcher.calls != 1 {
		t.Fatalf("active archive: %d %s; archived=%t, reads=%d", response.Code, response.Body.String(), store.rows[0].Archived, dispatcher.calls)
	}
	dispatcher.session.Status.Activity.GetObserved().ActiveTurnId = ""
	response = call(http.MethodPost, path+"/archive", `{}`, true)
	if response.Code != http.StatusOK || !store.rows[0].Archived || dispatcher.requestedID != id.String() || dispatcher.calls != 2 {
		t.Fatalf("archive: %d %s, session %q", response.Code, response.Body.String(), dispatcher.requestedID)
	}
	response = call(http.MethodPost, path+"/unarchive", `{}`, true)
	if response.Code != http.StatusOK || store.rows[0].Archived {
		t.Fatalf("unarchive: %d %s", response.Code, response.Body.String())
	}
}

func TestArchiveThreadWithoutHistoryCannotBypassDispatcher(t *testing.T) {
	id := uuid.New()
	for _, test := range []struct {
		name    string
		session *dispatcherpb.Session
		err     error
		want    codes.Code
	}{
		{name: "no Dispatcher Session", err: status.Error(codes.NotFound, "test missing Session"), want: codes.NotFound},
		{name: "unavailable", err: status.Error(codes.Unavailable, "test Dispatcher unavailable"), want: codes.Unavailable},
		{name: "refused", err: status.Error(codes.FailedPrecondition, "test activity unavailable"), want: codes.FailedPrecondition},
		{name: "missing response", want: codes.Unavailable},
		{name: "wrong Session", session: threadReadIdleSession(uuid.New()), want: codes.Unavailable},
		{name: "missing activity", session: &dispatcherpb.Session{SessionId: id.String()}, want: codes.FailedPrecondition},
	} {
		t.Run(test.name, func(t *testing.T) {
			store := &threadReadStoreFake{}
			dispatcher := &threadReadDispatcherFake{session: test.session, err: test.err}
			handler := threadhttp.NewHandler(threadhttp.Dependencies{Store: store, Dispatcher: dispatcher})
			_, err := handler.ArchiveThread(t.Context(), &apppb.ThreadMetadataTarget{ThreadId: id.String()})
			if status.Code(err) != test.want || len(store.rows) != 0 || dispatcher.calls != 1 || dispatcher.requestedID != id.String() {
				t.Fatalf("archive = %v, rows=%v, reads=%d, requested=%s", err, store.rows, dispatcher.calls, dispatcher.requestedID)
			}
		})
	}
}

// The App stores the operator's attention only after it establishes access to the requested Session.
func TestNoticeAcknowledgementScope(t *testing.T) {
	id := uuid.New()
	notice := &apppb.CommandNoticeRevision{CommandId: "c", ReceiptVersion: 40000000000000001}
	for _, tc := range []struct {
		name    string
		session *dispatcherpb.Session
		err     error
		code    codes.Code
	}{
		{"missing Session", nil, status.Error(codes.NotFound, "Session missing"), codes.NotFound},
		{"forbidden", nil, status.Error(codes.PermissionDenied, "Session forbidden"), codes.PermissionDenied},
		{"different Session", threadReadIdleSession(uuid.New()), nil, codes.Unavailable},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := threadhttp.NewHandler(threadhttp.Dependencies{Dispatcher: &threadReadDispatcherFake{session: tc.session, err: tc.err}})
			_, err := h.AcknowledgeCommandNotice(t.Context(), &apppb.AcknowledgeCommandNoticeRequest{ThreadId: id.String(), Notice: notice})
			if status.Code(err) != tc.code {
				t.Fatalf("acknowledge: %v", err)
			}
			_, err = h.BatchGetCommandNoticeAcknowledgements(t.Context(), &apppb.BatchGetCommandNoticeAcknowledgementsRequest{ThreadId: id.String(), Notices: []*apppb.CommandNoticeRevision{notice}})
			if status.Code(err) != tc.code {
				t.Fatalf("lookup: %v", err)
			}
			_, err = h.ListCommandNoticeAcknowledgements(t.Context(), &apppb.ListCommandNoticeAcknowledgementsRequest{ThreadId: id.String()})
			if status.Code(err) != tc.code {
				t.Fatalf("history: %v", err)
			}
		})
	}
}

func TestNoticeAcknowledgementGatewayRequiresBrowserLogin(t *testing.T) {
	fixture := testauth.New(t)
	server := newHTTPServer(fixture.Auth)
	h := threadhttp.NewHandler(threadhttp.Dependencies{})
	if err := h.RegisterRoutes(server); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(fixture.Auth)
	path := "/upstream/app/v1/threads/" + uuid.NewString() + "/notice-acknowledgements"
	for _, req := range []struct{ method, path string }{{"GET", path}, {"POST", path}, {"POST", path + ":batchGet"}} {
		for _, authorized := range []bool{false, true} {
			r := httptest.NewRequest(req.method, req.path, nil)
			want := http.StatusUnauthorized
			if authorized {
				fixture.Authorize(r)
				want = http.StatusForbidden
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, r)
			if rec.Code != want {
				t.Fatalf("%s %s: %d, want %d", req.method, req.path, rec.Code, want)
			}
		}
	}
}
