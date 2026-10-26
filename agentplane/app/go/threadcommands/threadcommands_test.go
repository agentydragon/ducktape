package threadcommands

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/danielgtaylor/huma/v2"
	"github.com/danielgtaylor/huma/v2/adapters/humago"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/proto"
)

type fakeThreads struct {
	thread *threads.Thread
	err    error
	calls  int
}

func (f *fakeThreads) GetThread(context.Context, uuid.UUID) (*threads.Thread, error) {
	f.calls++
	return f.thread, f.err
}

type fakeHistory struct {
	entry          *eventlogpb.EventEntry
	admissionErr   error
	admissionCalls int
	command        *commandpb.Command
	summary        historydata.FoldSummary
	summaryErr     error
	summaryCalls   int
}

func (f *fakeHistory) AdmittedCommand(_ context.Context, _ uuid.UUID, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	f.admissionCalls++
	f.command = proto.Clone(command).(*commandpb.Command)
	if f.admissionErr != nil {
		return nil, f.admissionErr
	}
	if f.entry != nil && !proto.Equal(f.entry.GetEvent().GetCommandAdmitted().GetCommand(), command) {
		return nil, historydata.ErrCommandConflict
	}
	return f.entry, nil
}

func (f *fakeHistory) GetSummary(context.Context, uuid.UUID) (historydata.FoldSummary, error) {
	f.summaryCalls++
	return f.summary, f.summaryErr
}

type fakeReadiness struct {
	err   error
	calls int
}

func (f *fakeReadiness) CommandReady(context.Context, uuid.UUID) error {
	f.calls++
	return f.err
}

type fakeDispatcher struct {
	requests []proto.Message
	err      error
}

func (f *fakeDispatcher) SubmitInput(_ context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) ChangeModel(_ context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.ChangeModelResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) ChangeReasoningEffort(_ context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.ChangeReasoningEffortResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId()}}, nil
}

func (f *fakeDispatcher) InterruptTurn(_ context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	f.requests = append(f.requests, proto.Clone(request))
	if f.err != nil {
		return nil, f.err
	}
	return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP}, nil
}

func testHandler(t *testing.T, dependencies Dependencies) http.Handler {
	t.Helper()
	mux := http.NewServeMux()
	api := humago.New(mux, huma.DefaultConfig("threadcommands test", "0"))
	if err := RegisterRoutes(api, dependencies); err != nil {
		t.Fatal(err)
	}
	return mux
}

func newDeps() (Dependencies, *fakeHistory, *fakeThreads, *fakeReadiness, *fakeDispatcher) {
	history := &fakeHistory{summary: historydata.FoldSummary{
		Present: true, Sandbox: "operator-sandbox", SandboxUID: "sandbox-uid",
		Feed: &historydata.FeedSnapshot{Attached: &runnerpb.Attached{Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CODEX}}},
	}}
	threadStore := &fakeThreads{thread: &threads.Thread{ID: uuid.MustParse("c63ad50d-b438-4f45-8777-5c7d53f1092d"), Archived: true}}
	readiness := &fakeReadiness{}
	dispatcher := &fakeDispatcher{}
	return Dependencies{
		Threads: threadStore, History: history, Readiness: readiness, Dispatcher: dispatcher,
		Models: map[string][]string{"HARNESS_CODEX": {"vendor/model"}},
	}, history, threadStore, readiness, dispatcher
}

func postJSON(t *testing.T, handler http.Handler, path, value string) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(value))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func TestCommandRoutesDispatchWithStableIDsAndPayloads(t *testing.T) {
	deps, history, _, readiness, dispatcher := newDeps()
	handler := testHandler(t, deps)
	threadID := "c63ad50d-b438-4f45-8777-5c7d53f1092d"
	cases := []struct {
		path string
		body string
		id   string
	}{
		{path: "/threads/" + threadID + "/submit-input", body: `{"commandId":"input-1","text":"hello"}`, id: "input-1"},
		{path: "/threads/" + threadID + "/change-model", body: `{"commandId":"model-1","model":"vendor/model"}`, id: "model-1"},
		{path: "/threads/" + threadID + "/change-reasoning-effort", body: `{"commandId":"effort-1","effort":"high"}`, id: "effort-1"},
		{path: "/threads/" + threadID + "/interrupt-turn", body: `{"commandId":"interrupt-1","turnId":"turn-7"}`, id: "interrupt-1"},
	}
	for _, test := range cases {
		response := postJSON(t, handler, test.path, test.body)
		if response.Code != http.StatusOK {
			t.Fatalf("POST %s returned %d: %s", test.path, response.Code, response.Body.String())
		}
		var decoded map[string]any
		if err := json.Unmarshal(response.Body.Bytes(), &decoded); err != nil {
			t.Fatal(err)
		}
		if decoded["kind"] != "dispatch" && decoded["kind"] != "interrupt_result" {
			t.Fatalf("POST %s returned %v", test.path, decoded)
		}
	}
	if readiness.calls != 4 || history.summaryCalls != 4 || len(dispatcher.requests) != 4 {
		t.Fatalf("readiness/summary/dispatcher calls = %d/%d/%d", readiness.calls, history.summaryCalls, len(dispatcher.requests))
	}
	if got := dispatcher.requests[0].(*dispatcherpb.SubmitInputRequest); got.GetCommandId() != "input-1" || got.GetText() != "hello" || got.GetSandbox() != "operator-sandbox" || got.GetSandboxUid() != "sandbox-uid" {
		t.Fatalf("SubmitInput request = %v", got)
	}
	if got := dispatcher.requests[1].(*dispatcherpb.ChangeModelRequest); got.GetCommandId() != "model-1" || got.GetModel() != "vendor/model" || got.GetSandboxUid() != "sandbox-uid" {
		t.Fatalf("ChangeModel request = %v", got)
	}
	if got := dispatcher.requests[2].(*dispatcherpb.ChangeReasoningEffortRequest); got.GetCommandId() != "effort-1" || got.GetEffort() != "high" || got.GetSandboxUid() != "sandbox-uid" {
		t.Fatalf("ChangeReasoningEffort request = %v", got)
	}
	if got := dispatcher.requests[3].(*dispatcherpb.InterruptTurnRequest); got.GetCommandId() != "interrupt-1" || got.GetTurnId() != "turn-7" || got.GetSandboxUid() != "sandbox-uid" {
		t.Fatalf("InterruptTurn request = %v", got)
	}
}

func TestNewCommandsRejectHistorySessionsWithoutSandboxIdentity(t *testing.T) {
	deps, _, _, _, dispatcher := newDeps()
	deps.History.(*fakeHistory).summary.SandboxUID = ""
	response := postJSON(t, testHandler(t, deps), "/threads/c63ad50d-b438-4f45-8777-5c7d53f1092d/submit-input", `{"commandId":"input-1","text":"hello"}`)
	if response.Code != http.StatusConflict || !strings.Contains(response.Body.String(), "no Sandbox identity") {
		t.Fatalf("submit without Sandbox identity returned %d: %s", response.Code, response.Body.String())
	}
	if len(dispatcher.requests) != 0 {
		t.Fatalf("submitted without Sandbox identity: %v", dispatcher.requests)
	}
}

func TestArchivedExactRetryBypassesDeletedSandboxAndConflictIsRejected(t *testing.T) {
	deps, history, threadStore, readiness, dispatcher := newDeps()
	threadStore.thread = nil // History admission is checked before app thread metadata.
	threadID := "c63ad50d-b438-4f45-8777-5c7d53f1092d"
	command := &commandpb.Command{CommandId: "retry-1", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "keep this"},
	}}
	history.entry = &eventlogpb.EventEntry{
		Cursor: 1,
		Origin: &eventlogpb.EventOrigin{SourceId: "runner", Sequence: 1},
		Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
			CommandAdmitted: &eventpb.CommandAdmitted{Command: command},
		}},
	}
	history.summaryErr = errors.New("the Sandbox target has been deleted")
	handler := testHandler(t, deps)
	path := "/threads/" + threadID + "/submit-input"
	response := postJSON(t, handler, path, `{"commandId":"retry-1","text":"keep this"}`)
	if response.Code != http.StatusOK {
		t.Fatalf("exact archived retry returned %d: %s", response.Code, response.Body.String())
	}
	var result map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result["kind"] != "archived_admission" || result["admission"] == nil {
		t.Fatalf("archived retry result = %v", result)
	}
	if readiness.calls != 0 || history.summaryCalls != 0 || threadStore.calls != 0 || len(dispatcher.requests) != 0 {
		t.Fatalf("exact retry called app store/summary/readiness/dispatcher: %d/%d/%d/%d", threadStore.calls, history.summaryCalls, readiness.calls, len(dispatcher.requests))
	}

	conflict := postJSON(t, handler, path, `{"commandId":"retry-1","text":"different"}`)
	if conflict.Code != http.StatusConflict || !strings.Contains(conflict.Body.String(), "already admitted with different work") {
		t.Fatalf("conflicting archived retry returned %d: %s", conflict.Code, conflict.Body.String())
	}
	if threadStore.calls != 0 || history.summaryCalls != 0 {
		t.Fatalf("conflicting retry reached app store/summary: %d/%d", threadStore.calls, history.summaryCalls)
	}
}

func TestNewCommandsNeedReadinessAndCompatibleModel(t *testing.T) {
	deps, history, _, readiness, dispatcher := newDeps()
	readiness.err = &appbridge.ThreadStateError{Message: "runner history is rejected: bad event"}
	handler := testHandler(t, deps)
	threadID := "c63ad50d-b438-4f45-8777-5c7d53f1092d"
	response := postJSON(t, handler, "/threads/"+threadID+"/submit-input", `{"commandId":"blocked","text":"hello"}`)
	if response.Code != http.StatusConflict || len(dispatcher.requests) != 0 {
		t.Fatalf("readiness failure returned %d and calls=%d: %s", response.Code, len(dispatcher.requests), response.Body.String())
	}
	readiness.err = nil
	response = postJSON(t, handler, "/threads/"+threadID+"/change-model", `{"commandId":"invalid-model","model":"not-offered"}`)
	if response.Code != http.StatusUnprocessableEntity || len(dispatcher.requests) != 0 || history.summaryCalls != 2 {
		t.Fatalf("incompatible model returned %d; dispatch=%d summary=%d: %s", response.Code, len(dispatcher.requests), history.summaryCalls, response.Body.String())
	}
}

func TestDispatcherDeadlineUsesCommandIDAndBodyRejectsUnknownFields(t *testing.T) {
	deps, _, _, _, dispatcher := newDeps()
	dispatcher.err = &appbridge.RequestTimeoutError{Cause: errors.New("deadline exceeded")}
	handler := testHandler(t, deps)
	threadID := "c63ad50d-b438-4f45-8777-5c7d53f1092d"
	response := postJSON(t, handler, "/threads/"+threadID+"/submit-input", `{"commandId":"retry-after-timeout","text":"hello"}`)
	if response.Code != http.StatusGatewayTimeout || !strings.Contains(response.Body.String(), "retry-after-timeout") {
		t.Fatalf("deadline returned %d: %s", response.Code, response.Body.String())
	}
	dispatcher.err = nil
	response = postJSON(t, handler, "/threads/"+threadID+"/submit-input", `{"commandId":"valid","text":"hello","extra":true}`)
	if response.Code != http.StatusUnprocessableEntity {
		t.Fatalf("unknown body field returned %d: %s", response.Code, response.Body.String())
	}
}

func TestDispatcherServiceStatusMapping(t *testing.T) {
	deps, _, _, _, dispatcher := newDeps()
	dispatcher.err = &appbridge.ServiceError{Code: codes.NotFound, Details: "sandbox was deleted"}
	handler := testHandler(t, deps)
	threadID := "c63ad50d-b438-4f45-8777-5c7d53f1092d"
	response := postJSON(t, handler, "/threads/"+threadID+"/submit-input", `{"commandId":"new","text":"hello"}`)
	if response.Code != http.StatusNotFound {
		t.Fatalf("NOT_FOUND returned %d: %s", response.Code, response.Body.String())
	}
}

var (
	_ ThreadStore      = (*fakeThreads)(nil)
	_ History          = (*fakeHistory)(nil)
	_ CommandReadiness = (*fakeReadiness)(nil)
	_ Dispatcher       = (*fakeDispatcher)(nil)
)
