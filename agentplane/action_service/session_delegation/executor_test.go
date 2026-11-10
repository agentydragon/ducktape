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
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/session_delegation/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/workload_auth/grpc"
	"github.com/google/uuid"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testCallerNamespace = "agentplane-staging"
	testCallerName      = "sandbox-one"
	testCallerUID       = "caller-uid-1"
	testSessionID       = "11111111-1111-4111-8111-111111111111"
	testCommandID       = "22222222-2222-4222-8222-222222222222"
)

type delegatedBackend struct {
	environmentpb.UnimplementedEnvironmentServiceServer
	dispatcherpb.UnimplementedSessionDispatcherServer
	historypb.UnimplementedHistoryServiceServer
	testing        *testing.T
	gotOpen        *dispatcherpb.CreateSessionRequest
	openErr        error
	openCalls      int
	gotSubmit      *dispatcherpb.SubmitInputRequest
	submitCalls    int
	interruptCalls int
	gotList        *environmentpb.ListEnvironmentsRequest
	gotGet         *environmentpb.GetEnvironmentRequest
	gotResume      *environmentpb.ResumeEnvironmentRequest
	resumeResponse *environmentpb.ResumeEnvironmentResponse
	resumeErr      error
	resumeCalls    int
	lookup         *dispatcherpb.Dispatch
	lookupErr      error
	cancels        int
	opening        *dispatcherpb.Session
	summary        *historypb.SessionSummary
	summaryErr     error
	readEvents     func(*historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
}

func (b *delegatedBackend) ListEnvironments(ctx context.Context, request *environmentpb.ListEnvironmentsRequest) (*environmentpb.ListEnvironmentsResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.gotList = proto.Clone(request).(*environmentpb.ListEnvironmentsRequest)
	return &environmentpb.ListEnvironmentsResponse{Items: []*environmentpb.EnvironmentDescriptor{{Target: &identitypb.EnvironmentTarget{Namespace: testCallerNamespace, Name: "sandbox-one", Uid: "sandbox-uid-1"}}}}, nil
}

func (b *delegatedBackend) GetEnvironment(ctx context.Context, request *environmentpb.GetEnvironmentRequest) (*environmentpb.EnvironmentDescriptor, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.gotGet = proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
	return &environmentpb.EnvironmentDescriptor{Target: proto.Clone(request.GetTarget()).(*identitypb.EnvironmentTarget)}, nil
}

func (b *delegatedBackend) ResumeEnvironment(ctx context.Context, request *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.resumeCalls++
	b.gotResume = proto.Clone(request).(*environmentpb.ResumeEnvironmentRequest)
	if b.resumeErr != nil {
		return nil, b.resumeErr
	}
	if b.resumeResponse != nil {
		return proto.Clone(b.resumeResponse).(*environmentpb.ResumeEnvironmentResponse), nil
	}
	return &environmentpb.ResumeEnvironmentResponse{
		Target:  proto.Clone(request.GetTarget()).(*identitypb.EnvironmentTarget),
		Outcome: environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED,
	}, nil
}

func (b *delegatedBackend) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	if b.opening == nil {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	return &dispatcherpb.GetSessionResponse{Session: proto.Clone(b.opening).(*dispatcherpb.Session)}, nil
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

func (b *delegatedBackend) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.openCalls++
	if b.openErr != nil {
		return nil, b.openErr
	}
	b.gotOpen = proto.Clone(request).(*dispatcherpb.CreateSessionRequest)
	return &dispatcherpb.CreateSessionResponse{Session: &dispatcherpb.Session{
		SessionId: request.GetSessionId(), Placement: request.GetSpec().GetPlacement(),
		Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING},
	}}, nil
}

func (b *delegatedBackend) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.submitCalls++
	b.gotSubmit = proto.Clone(request).(*dispatcherpb.SubmitInputRequest)
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: request.GetCommandId(), SessionId: request.GetSessionId(), Sandbox: "sandbox-one",
		SandboxUid: request.GetExpectedSandboxUid(), Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
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
		Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
	}}, nil
}

func (b *delegatedBackend) InterruptTurn(ctx context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	b.assertCaller(ctx, testRequestID, testExecutionID)
	b.interruptCalls++
	return &dispatcherpb.InterruptTurnResponse{Dispatch: &dispatcherpb.Dispatch{CommandId: request.GetCommandId(), SessionId: request.GetSessionId(), Operation: commandpb.Operation_OPERATION_INTERRUPT_TURN, State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER}}, nil
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
	environmentpb.RegisterEnvironmentServiceServer(server, backend)
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
	connection, err := workloadgrpc.DialDelegated(listener.Addr().String(), tokenFile, 2_000_000_000, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	executor, err := NewExecutor(group, catalog.SessionDelegationBinding{Description: "forwards caller"},
		environmentpb.NewEnvironmentServiceClient(connection),
		dispatcherpb.NewSessionDispatcherClient(connection), historypb.NewHistoryServiceClient(connection))
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
	getOutcome, err := executor.attempt(t.Context(), requestFor("get", map[string]any{
		"target": map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
	}, &uid))
	if err != nil || getOutcome.State != store.ExecutionSucceeded {
		t.Fatalf("get outcome=%+v error=%v", getOutcome, err)
	}
	if backend.gotGet == nil || backend.gotGet.GetTarget().GetNamespace() != testCallerNamespace || backend.gotGet.GetTarget().GetName() != "sandbox-one" ||
		backend.gotGet.GetTarget().GetUid() != "sandbox-uid-1" {
		t.Fatalf("GetEnvironment request = %v", backend.gotGet)
	}
}

func TestResumeForwardsRecordedCallerExactTargetAndOpaquePrecondition(t *testing.T) {
	backend := &delegatedBackend{testing: t, resumeResponse: &environmentpb.ResumeEnvironmentResponse{
		Target:  &identitypb.EnvironmentTarget{Namespace: testCallerNamespace, Name: "sandbox-one", Uid: "sandbox-uid-1"},
		Outcome: environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED,
	}}
	executor := newDelegatedExecutorForGroup(t, backend, contract.GroupEnvironments)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("resume", map[string]any{
		"target":       map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"resume_token": "opaque-generation-bound-token",
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	if backend.resumeCalls != 1 || backend.gotResume == nil || backend.gotResume.GetResumeToken() != "opaque-generation-bound-token" ||
		backend.gotResume.GetTarget().GetNamespace() != testCallerNamespace || backend.gotResume.GetTarget().GetName() != "sandbox-one" ||
		backend.gotResume.GetTarget().GetUid() != "sandbox-uid-1" {
		t.Fatalf("resume request=%v calls=%d", backend.gotResume, backend.resumeCalls)
	}
	var response struct {
		Outcome string `json:"outcome"`
	}
	if err := json.Unmarshal(outcome.Result, &response); err != nil {
		t.Fatalf("decode result: %v", err)
	}
	if response.Outcome != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED.String() {
		t.Fatalf("resume outcome=%q", response.Outcome)
	}
}

func TestResumeUnknownReplyIsNotReplayed(t *testing.T) {
	assessment := &environmentlifecyclepb.WakeAssessment{
		State:  environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
		Reason: environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN,
		Error:  &statuspb.Status{Code: int32(codes.Unavailable), Message: "patch result unknown"},
	}
	resumeStatus, err := status.New(codes.Unavailable, "private backend detail").WithDetails(assessment)
	if err != nil {
		t.Fatal(err)
	}
	backend := &delegatedBackend{testing: t, resumeErr: resumeStatus.Err()}
	executor := newDelegatedExecutorForGroup(t, backend, contract.GroupEnvironments)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("resume", map[string]any{
		"target":       map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"resume_token": "opaque-generation-bound-token",
	}, &uid))
	if err != nil || outcome.State != store.ExecutionUnknown || backend.resumeCalls != 1 {
		t.Fatalf("resume outcome=%+v error=%v calls=%d; want one unknown-outcome attempt", outcome, err, backend.resumeCalls)
	}
	var failure struct {
		Kind           string `json:"kind"`
		GRPCCode       string `json:"grpc_code"`
		WakeAssessment struct {
			State  string `json:"state"`
			Reason string `json:"reason"`
			Error  struct {
				Code    int32  `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		} `json:"wake_assessment"`
	}
	if err := json.Unmarshal(outcome.Error, &failure); err != nil {
		t.Fatal(err)
	}
	if failure.Kind != string(store.UnknownAdapterOutcome) || failure.GRPCCode != "UNAVAILABLE" ||
		failure.WakeAssessment.State != environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN.String() ||
		failure.WakeAssessment.Reason != environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN.String() ||
		failure.WakeAssessment.Error.Code != int32(codes.Unavailable) || failure.WakeAssessment.Error.Message != "patch result unknown" {
		t.Fatalf("unknown resume error details = %+v", failure)
	}
}

func TestResumePolicyDenialIsMappedToBackendRefusal(t *testing.T) {
	denial := &environmentlifecyclepb.ResumeEligibility{
		State:  environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED,
		Reason: environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED,
	}
	resumeStatus, err := status.New(codes.PermissionDenied, "private policy detail").WithDetails(denial)
	if err != nil {
		t.Fatal(err)
	}
	backend := &delegatedBackend{testing: t, resumeErr: resumeStatus.Err()}
	executor := newDelegatedExecutorForGroup(t, backend, contract.GroupEnvironments)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("resume", map[string]any{
		"target":       map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"resume_token": "opaque-generation-bound-token",
	}, &uid))
	if err != nil || outcome.State != store.ExecutionFailed || backend.resumeCalls != 1 {
		t.Fatalf("denied resume outcome=%+v error=%v calls=%d", outcome, err, backend.resumeCalls)
	}
	var failure struct {
		Kind              string `json:"kind"`
		GRPCCode          string `json:"grpc_code"`
		ResumeEligibility struct {
			State  string `json:"state"`
			Reason string `json:"reason"`
		} `json:"resume_eligibility"`
	}
	if err := json.Unmarshal(outcome.Error, &failure); err != nil {
		t.Fatal(err)
	}
	if failure.Kind != "backend_refused" || failure.GRPCCode != "PERMISSION_DENIED" ||
		failure.ResumeEligibility.State != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED.String() ||
		failure.ResumeEligibility.Reason != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED.String() ||
		strings.Contains(string(outcome.Error), "private policy detail") {
		t.Fatalf("denied resume error = %s", outcome.Error)
	}
}

func TestResumeRateLimitPreservesWakeAssessmentRetryTime(t *testing.T) {
	retryAt := timestamppb.New(time.Date(2026, time.November, 1, 12, 0, 0, 0, time.UTC))
	assessment := &environmentlifecyclepb.WakeAssessment{
		State:   environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED,
		Reason:  environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED,
		RetryAt: retryAt,
	}
	resumeStatus, err := status.New(codes.ResourceExhausted, "wake budget exhausted").WithDetails(assessment)
	if err != nil {
		t.Fatal(err)
	}
	backend := &delegatedBackend{testing: t, resumeErr: resumeStatus.Err()}
	executor := newDelegatedExecutorForGroup(t, backend, contract.GroupEnvironments)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("resume", map[string]any{
		"target":       map[string]string{"namespace": testCallerNamespace, "name": "sandbox-one", "uid": "sandbox-uid-1"},
		"resume_token": "opaque-generation-bound-token",
	}, &uid))
	if err != nil || outcome.State != store.ExecutionFailed || backend.resumeCalls != 1 {
		t.Fatalf("rate limited resume outcome=%+v error=%v calls=%d", outcome, err, backend.resumeCalls)
	}
	var failure struct {
		Kind           string `json:"kind"`
		GRPCCode       string `json:"grpc_code"`
		WakeAssessment struct {
			State   string `json:"state"`
			Reason  string `json:"reason"`
			RetryAt string `json:"retry_at"`
		} `json:"wake_assessment"`
	}
	if err := json.Unmarshal(outcome.Error, &failure); err != nil {
		t.Fatal(err)
	}
	if failure.Kind != "capacity_exhausted" || failure.GRPCCode != "RESOURCE_EXHAUSTED" ||
		failure.WakeAssessment.State != environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED.String() ||
		failure.WakeAssessment.Reason != environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED.String() ||
		failure.WakeAssessment.RetryAt != "2026-11-01T12:00:00Z" {
		t.Fatalf("rate limit resume error details = %+v; raw=%s", failure, outcome.Error)
	}
}

func TestEachExecutorOffersOnlyItsOwnActionGroup(t *testing.T) {
	for _, test := range []struct {
		group string
		want  []string
	}{
		{group: "environments", want: []string{"get", "list", "resume"}},
		{group: "sessions", want: []string{"cancel_input", "create", "get", "interrupt_turn", "observe_input", "read_turns", "submit_input"}},
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

func TestGetSessionRetainsObservedSessionAndSanitizedHistoryError(t *testing.T) {
	backend := &delegatedBackend{
		testing: t,
		opening: &dispatcherpb.Session{
			SessionId: testSessionID, Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_ACCEPTED},
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
		Session   struct {
			Kind  string          `json:"kind"`
			Value json.RawMessage `json:"value"`
		} `json:"session"`
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
	var session struct {
		SessionID string `json:"sessionId"`
		Status    struct {
			Materialization string `json:"materialization"`
		} `json:"status"`
	}
	if err := json.Unmarshal(result.Session.Value, &session); err != nil {
		t.Fatal(err)
	}
	if result.SessionID != testSessionID || result.Session.Kind != "observed" || session.SessionID != testSessionID ||
		session.Status.Materialization != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_ACCEPTED.String() {
		t.Fatalf("Dispatcher Session was not preserved: %+v session=%+v", result, session)
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
		Caller: store.ServiceAccountCaller{Account: store.ServiceAccount{Namespace: testCallerNamespace, Name: testCallerName}, UID: callerUID},
	}
}

func TestOpenForwardsRecordedCallerAndReturnsTypedSession(t *testing.T) {
	backend := &delegatedBackend{testing: t}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	request := requestFor("create", map[string]any{
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
	if backend.gotOpen == nil || backend.gotOpen.GetSessionId() != testSessionID || backend.gotOpen.GetSpec().GetPlacement().GetEnvironmentHosted().GetEnvironment().GetUid() != "sandbox-uid-1" ||
		backend.gotOpen.GetSpec().GetInstructions() != "Investigate the child task." || backend.gotOpen.GetSpec().GetCwd() != "/workspace/"+testSessionID {
		t.Fatalf("CreateSession request = %v", backend.gotOpen)
	}
	var result struct {
		Session struct {
			SessionID string `json:"session_id"`
			Placement struct {
				EnvironmentHosted struct {
					Environment struct {
						UID string `json:"uid"`
					} `json:"environment"`
				} `json:"environment_hosted"`
			} `json:"placement"`
			Status struct {
				Materialization string `json:"materialization"`
			} `json:"status"`
		} `json:"session"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Session.SessionID != testSessionID || result.Session.Placement.EnvironmentHosted.Environment.UID != "sandbox-uid-1" || result.Session.Status.Materialization != "SESSION_MATERIALIZATION_WAITING" {
		t.Fatalf("opening result = %+v", result.Session)
	}
}

func TestOpenUnknownOutcomeIsNotRetried(t *testing.T) {
	backend := &delegatedBackend{testing: t, openErr: status.Error(codes.Unavailable, "private backend detail")}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	_, err := executor.attempt(t.Context(), requestFor("create", map[string]any{
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
			name: "create",
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
		backend.gotSubmit.GetExpectedSandboxUid() != "sandbox-uid-1" || backend.gotSubmit.GetText() != "Do the requested work." {
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
				CommandId: testCommandID, SessionId: testSessionID, Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
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
		CommandId: testCommandID, SessionId: testSessionID, Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
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
	outcome, err := executor.attempt(t.Context(), requestFor("create", map[string]any{
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
