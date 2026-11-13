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

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"google.golang.org/grpc"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
	summaries   map[uuid.UUID]*historypb.SessionSummary
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
		uuid.MustParse("00000000-0000-4000-8000-000000000006"),
	}
	unknownTurnStatus := eventpb.TurnStatus(999)
	store := &threadReadStoreFake{rows: []threads.Thread{
		{ID: ids[0], CreatedAt: time.Unix(4, 0), Archived: true},
		{ID: ids[1], CreatedAt: time.Unix(3, 0)},
		{ID: ids[2], CreatedAt: time.Unix(2, 0)},
		{ID: ids[5], CreatedAt: time.Unix(1, 500_000_000), Archived: true},
		{ID: ids[3], CreatedAt: time.Unix(1, 0), Archived: true},
		{ID: ids[4], CreatedAt: time.Unix(0, 0), Archived: true},
	}}
	history := &threadReadHistoryFake{
		summaries: map[uuid.UUID]*historypb.SessionSummary{
			ids[0]: {Sandbox: &historypb.SandboxRef{Name: "reused", Uid: proto.String(oldUID)}},
			ids[1]: {Sandbox: &historypb.SandboxRef{Name: "reused", Uid: proto.String(currentUID)}, LastTurnStatus: &unknownTurnStatus},
			ids[2]: {Sandbox: &historypb.SandboxRef{Name: "reused", Uid: proto.String(currentUID)}},
			ids[3]: {Sandbox: &historypb.SandboxRef{Name: "reused", Uid: proto.String(currentUID)}},
			ids[5]: {Sandbox: &historypb.SandboxRef{Name: "reused", Uid: proto.String(oldUID)}},
		},
		sandboxes:   map[uuid.UUID]string{ids[0]: "reused", ids[1]: "reused", ids[2]: "reused", ids[3]: "reused", ids[4]: "reused", ids[5]: "reused"},
		sandboxUIDs: map[uuid.UUID]string{ids[0]: oldUID, ids[1]: currentUID, ids[2]: currentUID, ids[3]: currentUID, ids[4]: currentUID, ids[5]: currentUID},
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
	third, err := service.ListThreadMetadata(context.Background(), &apppb.ListThreadMetadataRequest{SandboxUid: currentUID, Limit: 1, IncludeArchived: true, PageToken: second.NextPageToken})
	if err != nil {
		t.Fatal(err)
	}
	if len(third.Threads) != 1 || third.Threads[0].ThreadId != ids[3].String() || third.NextPageToken != "" || third.ArchivedCount != 1 {
		t.Fatalf("third current-incarnation page = %+v, want the matching archived Thread only", third)
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
	target *identitypb.EnvironmentTarget
}

func (f *threadReadEnvironmentRuntimeFake) EnvironmentRuntimeStatus(_ context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	f.target = sandbox.GetTarget()
	return f.status, f.err
}

func (f *threadReadHistoryFake) GetSummary(_ context.Context, id uuid.UUID) (*historypb.SessionSummary, error) {
	return f.summaries[id], nil
}

func (f *threadReadHistoryFake) BatchGetSummaries(_ context.Context, ids []uuid.UUID) (map[uuid.UUID]*historypb.SessionSummary, error) {
	if f.err != nil {
		return nil, f.err
	}
	result := make(map[uuid.UUID]*historypb.SessionSummary, len(ids))
	for _, id := range ids {
		result[id] = f.summaries[id]
	}
	return result, nil
}

func (f *threadReadHistoryFake) FindSessions(_ context.Context, _, _, sandboxUID *string) (map[uuid.UUID]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	result := make(map[uuid.UUID]string)
	if sandboxUID != nil {
		for id, uid := range f.sandboxUIDs {
			if uid == *sandboxUID {
				result[id] = f.sandboxes[id]
			}
		}
	}
	return result, nil
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
		Target: &identitypb.EnvironmentTarget{Namespace: "team", Name: "sandbox", Uid: "sandbox-uid"},
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
		KubernetesDiagnostics: &environmentpb.KubernetesDiagnostics{Pod: &environmentpb.SandboxPod{Name: "environment-runtime-pod"}},
	}
	inventory := &threadReadInventoryFake{sandbox: sandbox}
	environmentRuntime := &threadReadEnvironmentRuntimeFake{status: &sessionpb.EnvironmentRuntimeStatus{Sessions: []*sessionpb.SessionStatus{{SessionId: id.String(), Busy: true}}}}
	summary := &historypb.SessionSummary{Sandbox: &historypb.SandboxRef{Name: "sandbox", Uid: proto.String("sandbox-uid")}}
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); !errors.Is(err, errThreadBusy) {
		t.Fatalf("busy Environment Runtime archive check = %v, want errThreadBusy", err)
	}
	if inventory.exactName != "sandbox" || inventory.exactUID != "sandbox-uid" {
		t.Fatalf("archive lookup identity = %q/%q, want exact Sandbox UID", inventory.exactName, inventory.exactUID)
	}
	if environmentRuntime.target.GetUid() != "sandbox-uid" {
		t.Fatalf("runtime status target = %v, want exact Sandbox UID", environmentRuntime.target)
	}
	environmentRuntime.status.Sessions[0].Busy = false
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); err != nil {
		t.Fatalf("idle Environment Runtime archive check = %v, want nil", err)
	}
	sandbox.ObservedState.OperatingMode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); !errors.Is(err, errThreadEnvironmentRuntimeUnready) {
		t.Fatalf("unready Environment Runtime archive check = %v, want errThreadEnvironmentRuntimeUnready", err)
	}
}

func TestEnsureThreadArchivableRequiresSandboxIdentity(t *testing.T) {
	thread := threads.Thread{ID: uuid.New()}
	for _, summary := range []*historypb.SessionSummary{
		{Sandbox: &historypb.SandboxRef{Uid: proto.String("uid")}},
		{Sandbox: &historypb.SandboxRef{Name: "sandbox"}},
	} {
		inventory := &threadReadInventoryFake{sandbox: &environmentpb.EnvironmentDescriptor{
			Target: &identitypb.EnvironmentTarget{Name: "sandbox", Uid: "uid"},
		}}
		if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, nil); err == nil || inventory.exactName != "" {
			t.Fatalf("archive validation for identity %+v = %v, lookup name %q; want rejection before lookup", summary, err, inventory.exactName)
		}
	}
}

func TestRenameThreadReturnsNotFoundWhenHistorySessionIsMissing(t *testing.T) {
	id := uuid.New()
	store := &threadReadStoreFake{rows: []threads.Thread{{ID: id}}}
	service := &appService{threadReader: store, threadHistory: &threadReadHistoryFake{}}
	name := "renamed"
	_, err := service.RenameThread(context.Background(), &apppb.RenameThreadRequest{ThreadId: id.String(), Name: &name})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("RenameThread: %v, want NotFound", err)
	}
	if store.rows[0].Name != nil {
		t.Fatal("renamed without a History session")
	}
}

func TestRenameThreadRequiresHistorySessionButNotSandboxIdentity(t *testing.T) {
	id := uuid.New()
	store := &threadReadStoreFake{rows: []threads.Thread{{ID: id}}}
	service := &appService{threadReader: store, threadHistory: &threadReadHistoryFake{
		summaries: map[uuid.UUID]*historypb.SessionSummary{id: {}},
	}}
	name := "renamed"
	metadata, err := service.RenameThread(context.Background(), &apppb.RenameThreadRequest{ThreadId: id.String(), Name: &name})
	if err != nil || metadata.GetName() != name {
		t.Fatalf("RenameThread with a present History Session and no Sandbox identity = %v, %v", metadata, err)
	}
}

func TestThreadMutationGateway(t *testing.T) {
	id := uuid.New()
	store := &threadReadStoreFake{rows: []threads.Thread{{ID: id, CreatedAt: time.Now()}}}
	unknownTurnStatus := eventpb.TurnStatus(999)
	history := &threadReadHistoryFake{summaries: map[uuid.UUID]*historypb.SessionSummary{id: {
		Sandbox: &historypb.SandboxRef{Name: "sandbox", Uid: proto.String("uid")}, LastTurnStatus: &unknownTurnStatus,
	}}}
	inventory := &threadReadInventoryFake{sandbox: &environmentpb.EnvironmentDescriptor{Target: &identitypb.EnvironmentTarget{Name: "sandbox", Uid: "uid"}}}
	authentication, err := auth.New(auth.Config{TokenReviews: threadReadAcceptedTokenReviewer{}, TokenAudience: "agentplane", TokenSubjects: []string{"system:serviceaccount:tools:runner"}})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	if err := registerThreadMetadataGateway(server, &appService{threadReader: store, threadHistory: history, environments: inventory}); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(authentication)
	path := "/upstream/app/v1/threads/" + id.String()
	call := func(method, path, body string, authorized bool) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if authorized {
			req.Header.Set("Authorization", "Bearer accepted")
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
	response = call(http.MethodPost, path+"/archive", `{}`, true)
	if response.Code != http.StatusOK || !store.rows[0].Archived || inventory.exactUID != "uid" {
		t.Fatalf("archive: %d %s, uid %q", response.Code, response.Body.String(), inventory.exactUID)
	}
	response = call(http.MethodPost, path+"/unarchive", `{}`, true)
	if response.Code != http.StatusOK || store.rows[0].Archived {
		t.Fatalf("unarchive: %d %s", response.Code, response.Body.String())
	}
}
