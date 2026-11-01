package sessiondelegation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/workload_auth/grpc"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	testCallerNamespace = "agentplane-staging"
	testCallerName      = "sandbox-one"
	testCallerUID       = "caller-uid-1"
	testSessionID       = "11111111-1111-4111-8111-111111111111"
	testCommandID       = "22222222-2222-4222-8222-222222222222"
)

type delegatedBackend struct {
	discoverypb.UnimplementedEnvironmentDiscoveryServer
	dispatcherpb.UnimplementedSessionDispatcherServer
	historypb.UnimplementedHistoryServiceServer
	testing        *testing.T
	gotOpen        *dispatcherpb.OpenSessionRequest
	openErr        error
	openCalls      int
	gotSubmit      *dispatcherpb.SubmitInputRequest
	submitCalls    int
	interruptCalls int
	gotList        *discoverypb.ListEnvironmentsRequest
	gotGet         *discoverypb.GetEnvironmentRequest
	lookup         *dispatcherpb.Dispatch
	lookupErr      error
	cancels        int
	opening        *dispatcherpb.SessionOpening
	summary        *historypb.SessionSummary
	summaryErr     error
	readEvents     func(*historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
}

func (b *delegatedBackend) ListEnvironments(ctx context.Context, request *discoverypb.ListEnvironmentsRequest) (*discoverypb.ListEnvironmentsResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.gotList = proto.Clone(request).(*discoverypb.ListEnvironmentsRequest)
	return &discoverypb.ListEnvironmentsResponse{Items: []*discoverypb.EnvironmentDescriptor{{Target: &discoverypb.TargetHandle{Namespace: testCallerNamespace, Name: "sandbox-one", Uid: "sandbox-uid-1"}}}}, nil
}

func (b *delegatedBackend) GetEnvironment(ctx context.Context, request *discoverypb.GetEnvironmentRequest) (*discoverypb.EnvironmentDescriptor, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.gotGet = proto.Clone(request).(*discoverypb.GetEnvironmentRequest)
	return &discoverypb.EnvironmentDescriptor{Target: proto.Clone(request.GetTarget()).(*discoverypb.TargetHandle)}, nil
}

func (b *delegatedBackend) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	if b.opening == nil {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	return &dispatcherpb.GetSessionOpeningResponse{Opening: proto.Clone(b.opening).(*dispatcherpb.SessionOpening)}, nil
}

func (b *delegatedBackend) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	if b.summaryErr != nil {
		return nil, b.summaryErr
	}
	if b.summary == nil {
		return nil, status.Error(codes.NotFound, "summary not found")
	}
	return proto.Clone(b.summary).(*historypb.SessionSummary), nil
}

func (b *delegatedBackend) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	if b.readEvents == nil {
		return nil, status.Error(codes.NotFound, "history not found")
	}
	return b.readEvents(request)
}

func (b *delegatedBackend) assertCaller(ctx context.Context, wantRequest, wantExecution uuid.UUID) {
	b.testing.Helper()
	metadata, ok := metadata.FromIncomingContext(ctx)
	if !ok || len(metadata.Get("authorization")) != 1 || metadata.Get("authorization")[0] != "Agentplane-Delegated delegated-test-token" {
		b.testing.Fatalf("authorization metadata = %v", metadata.Get("authorization"))
	}
	values := metadata.Get(auth.DelegationMetadataKey)
	if len(values) != 1 {
		b.testing.Fatalf("delegation metadata values = %d", len(values))
	}
	var envelope delegationpb.Delegation
	if err := proto.Unmarshal([]byte(values[0]), &envelope); err != nil {
		b.testing.Fatalf("unmarshal delegation envelope: %v", err)
	}
	if envelope.GetCaller().GetAccount().GetNamespace() != testCallerNamespace || envelope.GetCaller().GetAccount().GetName() != testCallerName || envelope.GetCaller().GetUid() != testCallerUID ||
		envelope.GetActionRequestId() != wantRequest.String() || envelope.GetActionExecutionId() != wantExecution.String() {
		b.testing.Fatalf("delegation envelope = %v", &envelope)
	}
}

func (b *delegatedBackend) OpenSession(ctx context.Context, request *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.openCalls++
	if b.openErr != nil {
		return nil, b.openErr
	}
	b.gotOpen = proto.Clone(request).(*dispatcherpb.OpenSessionRequest)
	return &dispatcherpb.OpenSessionResponse{Opening: &dispatcherpb.SessionOpening{
		SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(), SandboxUid: request.GetSandboxUid(),
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (b *delegatedBackend) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.submitCalls++
	b.gotSubmit = proto.Clone(request).(*dispatcherpb.SubmitInputRequest)
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), SessionId: request.GetSessionId(), Sandbox: request.GetSandbox(),
		SandboxUid: request.GetSandboxUid(), Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (b *delegatedBackend) GetDispatch(ctx context.Context, request *dispatcherpb.GetDispatchRequest) (*dispatcherpb.GetDispatchResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	if b.lookupErr != nil {
		return nil, b.lookupErr
	}
	if b.lookup == nil || b.lookup.GetCommandId() != request.GetCommandId() {
		return nil, status.Error(codes.NotFound, "dispatch not found")
	}
	return &dispatcherpb.GetDispatchResponse{Dispatch: proto.Clone(b.lookup).(*dispatcherpb.Dispatch)}, nil
}

func (b *delegatedBackend) CancelDispatch(ctx context.Context, request *dispatcherpb.CancelDispatchRequest) (*dispatcherpb.CancelDispatchResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.cancels++
	if request.GetCommandId() != testCommandID {
		return nil, status.Error(codes.InvalidArgument, "wrong command")
	}
	return &dispatcherpb.CancelDispatchResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), SessionId: testSessionID,
		Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
	}}, nil
}

func (b *delegatedBackend) InterruptTurn(ctx context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.interruptCalls++
	return &dispatcherpb.InterruptTurnResponse{}, nil
}

type unavailableHistoryClient struct{ historypb.HistoryServiceClient }

func (unavailableHistoryClient) FollowSession(
	context.Context,
	*historypb.FollowSessionRequest,
	...grpc.CallOption,
) (historypb.HistoryService_FollowSessionClient, error) {
	return nil, status.Error(codes.Unavailable, "private history detail")
}

var (
	testRequestID   = uuid.MustParse("33333333-3333-4333-8333-333333333333")
	testExecutionID = uuid.MustParse("44444444-4444-4444-8444-444444444444")
)

func newDelegatedExecutor(t *testing.T, backend *delegatedBackend) *Executor {
	return newDelegatedExecutorForGroup(t, backend, "sessions")
}

func newDelegatedExecutorForGroup(t *testing.T, backend *delegatedBackend, group string) *Executor {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	discoverypb.RegisterEnvironmentDiscoveryServer(server, backend)
	dispatcherpb.RegisterSessionDispatcherServer(server, backend)
	historypb.RegisterHistoryServiceServer(server, backend)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	tokenFile := filepath.Join(t.TempDir(), "dispatcher-token")
	if err := os.WriteFile(tokenFile, []byte("delegated-test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	connection, err := workloadgrpc.DialDelegated(listener.Addr().String(), tokenFile, 2_000_000_000)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	executor, err := NewExecutor(group, catalog.SessionDelegationBinding{Description: "forwards caller"},
		discoverypb.NewEnvironmentDiscoveryClient(connection), dispatcherpb.NewSessionDispatcherClient(connection), historypb.NewHistoryServiceClient(connection))
	if err != nil {
		t.Fatal(err)
	}
	return executor
}

func TestEnvironmentReadsForwardRecordedCallerAndExactTarget(t *testing.T) {
	backend := &delegatedBackend{testing: t}
	executor := newDelegatedExecutorForGroup(t, backend, "environments")
	uid := testCallerUID
	listOutcome, err := executor.attempt(t.Context(), requestFor("list", map[string]any{"limit": 7, "after": "cursor-1"}, &uid))
	if err != nil || listOutcome.State != store.ExecutionSucceeded {
		t.Fatalf("list outcome=%+v error=%v", listOutcome, err)
	}
	if backend.gotList == nil || backend.gotList.GetLimit() != 7 || backend.gotList.GetPageToken() != "cursor-1" {
		t.Fatalf("ListEnvironments request = %v", backend.gotList)
	}
	getOutcome, err := executor.attempt(t.Context(), requestFor("get", map[string]any{"target": map[string]string{
		"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1",
	}}, &uid))
	if err != nil || getOutcome.State != store.ExecutionSucceeded {
		t.Fatalf("get outcome=%+v error=%v", getOutcome, err)
	}
	if backend.gotGet == nil || backend.gotGet.GetTarget().GetNamespace() != testCallerNamespace || backend.gotGet.GetTarget().GetName() != "sandbox-one" || backend.gotGet.GetTarget().GetUid() != "sandbox-uid-1" {
		t.Fatalf("GetEnvironment request = %v", backend.gotGet)
	}
}

func TestEachExecutorOffersOnlyItsOwnActionGroup(t *testing.T) {
	for _, test := range []struct {
		group string
		want  []string
	}{
		{group: "environments", want: []string{"get", "list"}},
		{group: "sessions", want: []string{"cancel_input", "get", "interrupt_turn", "observe_input", "open", "read_turns", "submit_input"}},
	} {
		t.Run(test.group, func(t *testing.T) {
			executor, err := NewExecutor(test.group, catalog.SessionDelegationBinding{Description: "forwards caller"}, nil, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			got := slices.Sorted(maps.Keys(executor.Actions()))
			if !slices.Equal(got, test.want) {
				t.Fatalf("offered actions = %v, want %v", got, test.want)
			}
			if test.group == "sessions" {
				for _, name := range []string{"get", "read_turns"} {
					definition := executor.Actions()[name]
					if len(definition.InputSchema) == 0 {
						t.Errorf("%s has no advertised input schema", name)
					}
				}
			}
		})
	}
}

func TestGetSessionRetainsObservedOpeningAndSanitizedHistoryError(t *testing.T) {
	backend := &delegatedBackend{
		testing: t,
		opening: &dispatcherpb.SessionOpening{
			SessionId: testSessionID, State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
		},
		summaryErr: status.Error(codes.Unavailable, "private history detail"),
	}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("get", map[string]any{"session_id": testSessionID}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	var result struct {
		SessionID string `json:"session_id"`
		Opening   struct {
			Kind  string          `json:"kind"`
			Value json.RawMessage `json:"value"`
		} `json:"opening"`
		History struct {
			Kind  string `json:"kind"`
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		} `json:"history"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	var opening struct {
		SessionID string `json:"sessionId"`
		State     string `json:"state"`
	}
	if err := json.Unmarshal(result.Opening.Value, &opening); err != nil {
		t.Fatal(err)
	}
	if result.SessionID != testSessionID || result.Opening.Kind != "observed" || opening.SessionID != testSessionID ||
		opening.State != dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED.String() {
		t.Fatalf("Dispatcher opening was not preserved: %+v opening=%+v", result, opening)
	}
	if result.History.Kind != "unavailable" || result.History.Error.Code != codes.Unavailable.String() ||
		result.History.Error.Message != "service unavailable" {
		t.Fatalf("History source error = %+v", result.History)
	}
}

func TestReadTurnsReturnsPartialTurnsAndSanitizedHistoryError(t *testing.T) {
	const historyTail = uint64(1003)
	backend := &delegatedBackend{testing: t}
	backend.readEvents = func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		if request.GetAfterCursor() == 1000 {
			return nil, status.Error(codes.Unavailable, "private history detail")
		}
		response := &historypb.ReadSessionEventsResponse{LastCursor: historyTail}
		for cursor := request.GetAfterCursor() + 1; cursor <= historyTail && uint32(len(response.Entries)) < request.GetLimit(); cursor++ {
			var observation *eventpb.Event
			switch cursor {
			case 1:
				observation = &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "first-turn"}}}
			case 2:
				observation = &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
					TurnId: "first-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED,
				}}}
			case 3:
				observation = &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "partial-turn"}}}
			case historyTail:
				observation = &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
					TurnId: "partial-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED,
				}}}
			default:
				observation = &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
					ItemId: fmt.Sprintf("tool-%d", cursor), Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "exec",
				}}}
			}
			response.Entries = append(response.Entries, &eventlogpb.EventEntry{Cursor: cursor, Event: observation})
		}
		return response, nil
	}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("read_turns", map[string]any{
		"session_id": testSessionID, "count": 2,
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	var result struct {
		Turns []struct {
			TurnID    string `json:"turn_id"`
			ToolCalls []struct {
				Name string `json:"name"`
			} `json:"tool_calls"`
			Status *string `json:"status"`
		} `json:"turns"`
		ThroughCursor string `json:"through_cursor"`
		NextCursor    string `json:"next_cursor"`
		Error         struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Turns) != 2 || result.Turns[0].TurnID != "first-turn" || result.Turns[0].Status == nil ||
		*result.Turns[0].Status != eventpb.TurnStatus_TURN_STATUS_COMPLETED.String() {
		t.Fatalf("completed turn facts = %+v", result.Turns)
	}
	if result.Turns[1].TurnID != "partial-turn" || result.Turns[1].Status != nil || len(result.Turns[1].ToolCalls) != 997 {
		t.Fatalf("partial turn facts were lost or finalized: id=%q status=%v tool_calls=%d", result.Turns[1].TurnID, result.Turns[1].Status, len(result.Turns[1].ToolCalls))
	}
	if result.ThroughCursor != "1000" || result.NextCursor == "" || result.Error.Code != codes.Unavailable.String() ||
		result.Error.Message != "service unavailable" {
		t.Fatalf("partial History page = %+v", result)
	}
}

func requestFor(action string, args any, callerUID *string) store.ExecutionRequest {
	encoded, err := json.Marshal(args)
	if err != nil {
		panic(err)
	}
	return store.ExecutionRequest{
		RequestID: testRequestID, ExecutionID: testExecutionID,
		Action: store.ActionIdentity{Group: "sessions", Name: action}, Arguments: encoded,
		Caller: store.ServiceAccount{Namespace: testCallerNamespace, Name: testCallerName}, CallerUID: callerUID,
	}
}

func TestOpenForwardsRecordedCallerAndReturnsTypedOpening(t *testing.T) {
	backend := &delegatedBackend{testing: t}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	request := requestFor("open", map[string]any{
		"target":                  map[string]string{"namespace": testCallerNamespace, "name": testCallerName, "uid": "sandbox-uid-1"},
		"session_id":              testSessionID,
		"spec":                    map[string]any{"harness": "HARNESS_CLAUDE", "model": "claude-sonnet", "cwd": "/workspace/{session_id}"},
		"additional_instructions": "Investigate the child task.",
	}, &uid)
	outcome, err := executor.attempt(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome state = %s, want succeeded (%s)", outcome.State, outcome.Error)
	}
	if backend.gotOpen == nil || backend.gotOpen.GetSessionId() != testSessionID || backend.gotOpen.GetSandboxUid() != "sandbox-uid-1" ||
		backend.gotOpen.GetSpec().GetInstructions() != "Investigate the child task." || backend.gotOpen.GetSpec().GetCwd() != "/workspace/"+testSessionID {
		t.Fatalf("OpenSession request = %v", backend.gotOpen)
	}
	var result struct {
		Opening struct {
			SessionID  string `json:"session_id"`
			SandboxUID string `json:"sandbox_uid"`
			State      string `json:"state"`
		} `json:"opening"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Opening.SessionID != testSessionID || result.Opening.SandboxUID != "sandbox-uid-1" || result.Opening.State != "DELIVERY_STATE_QUEUED_IN_DISPATCHER" {
		t.Fatalf("opening result = %+v", result.Opening)
	}
}

func TestOpenUnknownOutcomeIsNotRetried(t *testing.T) {
	backend := &delegatedBackend{testing: t, openErr: status.Error(codes.Unavailable, "private backend detail")}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	_, err := executor.attempt(t.Context(), requestFor("open", map[string]any{
		"target":     map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"session_id": testSessionID,
		"spec":       map[string]any{"harness": "HARNESS_CLAUDE", "model": "model", "cwd": "/workspace"},
	}, &uid))
	if !errors.Is(err, worker.ErrOutcomeUnknown) || backend.openCalls != 1 {
		t.Fatalf("open error=%v, calls=%d; want one unknown-outcome attempt", err, backend.openCalls)
	}
}

func TestSessionWritesRejectTargetOutsideAuthenticatedCallerNamespace(t *testing.T) {
	for _, test := range []struct {
		name string
		args map[string]any
	}{
		{
			name: "open",
			args: map[string]any{
				"target":     map[string]string{"namespace": "other-namespace", "name": "sandbox-one", "uid": "sandbox-uid-1"},
				"session_id": testSessionID,
				"spec":       map[string]any{"harness": "HARNESS_CLAUDE", "model": "model", "cwd": "/workspace"},
			},
		},
		{
			name: "submit_input",
			args: map[string]any{
				"target":     map[string]string{"namespace": "other-namespace", "name": "sandbox-one", "uid": "sandbox-uid-1"},
				"session_id": testSessionID, "command_id": testCommandID, "text": "hello",
			},
		},
		{
			name: "interrupt_turn",
			args: map[string]any{
				"target":     map[string]string{"namespace": "other-namespace", "name": "sandbox-one", "uid": "sandbox-uid-1"},
				"session_id": testSessionID, "command_id": testCommandID, "turn_id": "turn-1",
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			backend := &delegatedBackend{testing: t}
			executor := newDelegatedExecutor(t, backend)
			uid := testCallerUID
			outcome, err := executor.attempt(t.Context(), requestFor(test.name, test.args, &uid))
			if err != nil {
				t.Fatal(err)
			}
			var failure struct {
				Kind string `json:"kind"`
			}
			if err := json.Unmarshal(outcome.Error, &failure); err != nil {
				t.Fatalf("decode Action error %s: %v", outcome.Error, err)
			}
			if outcome.State != store.ExecutionFailed || failure.Kind != "invalid_arguments" {
				t.Fatalf("outcome=%+v error=%v; want invalid_arguments", outcome, err)
			}
			if backend.openCalls != 0 || backend.submitCalls != 0 || backend.interruptCalls != 0 {
				t.Fatalf("wrong-namespace request reached a backend: open=%d submit=%d interrupt=%d", backend.openCalls, backend.submitCalls, backend.interruptCalls)
			}
		})
	}
}

func TestSubmitInputForwardsTheCallerAndDurableCommandIdentity(t *testing.T) {
	backend := &delegatedBackend{testing: t}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("submit_input", map[string]any{
		"target":     map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"session_id": testSessionID, "command_id": testCommandID, "text": "Do the requested work.",
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	if backend.gotSubmit == nil || backend.gotSubmit.GetSessionId() != testSessionID || backend.gotSubmit.GetCommandId() != testCommandID ||
		backend.gotSubmit.GetSandbox() != "sandbox-one" || backend.gotSubmit.GetSandboxUid() != "sandbox-uid-1" || backend.gotSubmit.GetText() != "Do the requested work." {
		t.Fatalf("SubmitInput request = %v", backend.gotSubmit)
	}
	var result struct {
		Dispatch struct {
			CommandID string `json:"command_id"`
			State     string `json:"state"`
		} `json:"dispatch"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Dispatch.CommandID != testCommandID || result.Dispatch.State != "DELIVERY_STATE_QUEUED_IN_DISPATCHER" {
		t.Fatalf("dispatch result = %+v", result.Dispatch)
	}
}

func TestCancelRequiresTheDurableReceiptToMatchTheSuppliedSession(t *testing.T) {
	for _, test := range []struct {
		name          string
		sessionID     string
		wantCancels   int
		wantSucceeded bool
	}{
		{name: "matching receipt", sessionID: testSessionID, wantCancels: 1, wantSucceeded: true},
		{name: "different session", sessionID: "55555555-5555-4555-8555-555555555555", wantCancels: 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			backend := &delegatedBackend{testing: t, lookup: &dispatcherpb.Dispatch{
				CommandId: testCommandID, SessionId: testSessionID, Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
				State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
			}}
			executor := newDelegatedExecutor(t, backend)
			uid := testCallerUID
			outcome, err := executor.attempt(t.Context(), requestFor("cancel_input", map[string]string{
				"session_id": test.sessionID, "command_id": testCommandID,
			}, &uid))
			if err != nil {
				t.Fatal(err)
			}
			if backend.cancels != test.wantCancels || (outcome.State == store.ExecutionSucceeded) != test.wantSucceeded {
				t.Fatalf("outcome=%+v cancel calls=%d, want success %v and %d cancel calls", outcome, backend.cancels, test.wantSucceeded, test.wantCancels)
			}
		})
	}
}

func TestObservePreservesTypedPartialSourceError(t *testing.T) {
	backend := &delegatedBackend{testing: t, lookupErr: status.Error(codes.Unavailable, "private backend detail")}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("observe_input", map[string]any{
		"session_id": testSessionID, "command_id": testCommandID, "wait_seconds": 0,
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	var result struct {
		Dispatch struct {
			Kind  string `json:"kind"`
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		} `json:"dispatch"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Dispatch.Kind != "partial" || result.Dispatch.Error.Code != codes.Unavailable.String() || result.Dispatch.Error.Message != "service unavailable" {
		t.Fatalf("dispatch partial = %+v", result.Dispatch)
	}
}

func TestObserveKeepsHistoryFailureInObservationSource(t *testing.T) {
	backend := &delegatedBackend{testing: t, lookup: &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID, Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
		Admission: &eventlogpb.EventEntry{Cursor: 2},
	}}
	executor := newDelegatedExecutor(t, backend)
	executor.history = unavailableHistoryClient{}
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("observe_input", map[string]any{
		"session_id": testSessionID, "command_id": testCommandID, "wait_seconds": 0,
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	var result struct {
		Dispatch struct {
			Kind  string `json:"kind"`
			Error any    `json:"error"`
		} `json:"dispatch"`
		Observation struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		} `json:"observation"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Dispatch.Kind != "observed" || result.Dispatch.Error != nil {
		t.Fatalf("Dispatcher fact = %+v, want observed with no error", result.Dispatch)
	}
	if result.Observation.Error.Code != codes.Unavailable.String() || result.Observation.Error.Message != "service unavailable" {
		t.Fatalf("History error = %+v", result.Observation.Error)
	}
}

func TestMissingCallerUIDFailsClosedBeforeBackendCall(t *testing.T) {
	backend := &delegatedBackend{testing: t}
	executor := newDelegatedExecutor(t, backend)
	outcome, err := executor.attempt(t.Context(), requestFor("open", map[string]any{
		"target":     map[string]string{"namespace": testCallerNamespace, "name": testCallerName, "uid": "sandbox-uid-1"},
		"session_id": testSessionID,
		"spec":       map[string]any{"harness": "HARNESS_CLAUDE", "model": "claude-sonnet", "cwd": "/workspace"},
	}, nil))
	if err != nil {
		t.Fatal(err)
	}
	if outcome.State != store.ExecutionFailed || backend.gotOpen != nil {
		t.Fatalf("outcome=%+v backend opening=%v; missing caller UID must fail before RPC", outcome, backend.gotOpen)
	}
}
