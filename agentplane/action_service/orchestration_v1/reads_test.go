package orchestrationv1

import (
	"fmt"
	"reflect"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var testEpoch = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

// summaryOf is a summary of a Session that sandbox wrote, first written at first.
func summaryOf(f fixture, sessionID string, sandbox contract.SandboxRef, first *time.Time) *historypb.SessionSummary {
	summary := &historypb.SessionSummary{
		SessionId: sessionID,
		Sandbox:   &historypb.SandboxRef{Namespace: f.namespace, Name: sandbox.Name, Uid: new(string(sandbox.UID))},
	}
	if first != nil {
		summary.FirstEventAt = timestamppb.New(*first)
	}
	return summary
}

func TestGetSessionReadsTheLaunchedIncarnationsSession(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	var sandbox sandboxapi.Sandbox
	if err := kube.Get(t.Context(), client.ObjectKey{Namespace: f.namespace, Name: launched.Name}, &sandbox); err != nil {
		t.Fatal(err)
	}
	meta.SetStatusCondition(&sandbox.Status.Conditions, metav1.Condition{
		Type: sandboxapi.ConditionReady, Status: metav1.ConditionFalse, Reason: "TestStarting", Message: "test message",
	})
	if err := kube.Status().Update(t.Context(), &sandbox); err != nil {
		t.Fatal(err)
	}
	session := uuid.NewString()
	at := timestamppb.New(testEpoch)
	f.dispatcher.hold(&dispatcherpb.SessionOpening{
		SessionId: session, Sandbox: launched.Name, SandboxUid: string(launched.UID),
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, CreatedAt: at, UpdatedAt: at,
	})
	var commands []string
	for range contract.RecentDispatches + 2 {
		command := uuid.NewString()
		commands = append(commands, command)
		f.dispatcher.hold(nil, &dispatcherpb.Dispatch{
			CommandId: command, SessionId: session, Sandbox: launched.Name, SandboxUid: string(launched.UID),
			Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
			State:     dispatcherpb.DeliveryState_DELIVERY_STATE_HARNESS_CONFIRMED, CreatedAt: at, UpdatedAt: at,
		})
	}
	summary := summaryOf(f, session, launched, &testEpoch)
	summary.FeedStatus = historypb.FeedStatus_FEED_STATUS_ACTIVE
	summary.LastTurnStatus = new(eventpb.TurnStatus_TURN_STATUS_COMPLETED)
	summary.Attached = &runnerpb.Attached{
		SessionId: session, ActiveTurnId: "test-turn", HarnessState: runnerpb.HarnessState_HARNESS_STATE_RUNNING,
		LaunchOutcome: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
		Spec:          &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CODEX, Cwd: "/test", Instructions: "test"},
	}
	f.history.hold(summary)
	f.history.follow = func(_ *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		return stream.Send(&historypb.FollowSessionResponse{Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	}

	state := resultOf[SessionState](f.attempt(t, f.caller, contract.ActionGetSession,
		fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}, "session_id": %q}`, launched.Name, launched.UID, session)))
	if ready := state.SandboxReady; ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != "TestStarting" {
		t.Errorf("sandbox_ready = %+v, want the Sandbox's Ready condition", ready)
	}
	if opening := state.Opening; opening == nil || opening.State != "DELIVERY_STATE_RUNNER_ADMITTED" {
		t.Errorf("opening = %+v, want the Session's opening", opening)
	}
	runner := state.Session.Runner
	if runner == nil || runner.ActiveTurnID == nil || *runner.ActiveTurnID != "test-turn" || runner.Harness != "HARNESS_CODEX" {
		t.Errorf("session.runner = %+v, want the running turn of the Codex harness", runner)
	}
	if turn := state.Session.LastTurnStatus; turn == nil || *turn != "TURN_STATUS_COMPLETED" {
		t.Errorf("session.last_turn_status = %v, want the last turn's", turn)
	}
	if len(state.Dispatches) != contract.RecentDispatches || state.Dispatches[0].CommandID != commands[len(commands)-1] ||
		state.Dispatches[contract.RecentDispatches-1].CommandID != commands[2] {
		t.Errorf("dispatches = %+v, want the %d most recent, newest first", state.Dispatches, contract.RecentDispatches)
	}
}

// A grant on one incarnation reads only its Sessions, whichever of the Session Dispatcher and the History Service knows
// the Session.
func TestGetSessionRefusesASessionItCannotTieToTheIncarnation(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	other := contract.SandboxRef{Name: launched.Name, UID: types.UID(uuid.NewString())}
	for name, test := range map[string]struct {
		opened, written *contract.SandboxRef
		found           bool
	}{
		"opened and written here":             {&launched, &launched, true},
		"only opened here":                    {&launched, nil, true},
		"only written here":                   {nil, &launched, true},
		"known to neither":                    {nil, nil, false},
		"opened by another incarnation":       {&other, nil, false},
		"written by another incarnation":      {nil, &other, false},
		"opened here, written by another one": {&launched, &other, false},
	} {
		t.Run(name, func(t *testing.T) {
			session := uuid.NewString()
			if test.opened != nil {
				f.dispatcher.hold(&dispatcherpb.SessionOpening{
					SessionId: session, Sandbox: test.opened.Name, SandboxUid: string(test.opened.UID),
					State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
				})
			}
			if test.written != nil {
				f.history.hold(summaryOf(f, session, *test.written, nil))
			}
			attempt := f.attempt(t, f.caller, contract.ActionGetSession,
				fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}, "session_id": %q}`, launched.Name, launched.UID, session))
			if !test.found {
				if kind := attempt.failure(); kind != "session_not_found" {
					t.Errorf("get_session failed %q, want session_not_found", kind)
				}
				return
			}
			resultOf[SessionState](attempt)
		})
	}
}

func TestListSessionsListsTheIncarnationsSessionsNewestFirst(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	// More than one summary read takes, so the listing reads them in batches.
	var written []string
	for index := range summaryBatch {
		session := uuid.NewString()
		written = append(written, session)
		f.history.hold(summaryOf(f, session, launched, new(testEpoch.Add(time.Duration(index)*time.Minute))))
	}
	unfolded := uuid.NewString()
	f.history.hold(
		summaryOf(f, unfolded, launched, nil),
		summaryOf(f, uuid.NewString(), contract.SandboxRef{Name: launched.Name, UID: types.UID(uuid.NewString())}, &testEpoch),
	)

	sandbox := fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}}`, launched.Name, launched.UID)
	listed := resultOf[SessionList](f.attempt(t, f.caller, contract.ActionListSessions, sandbox)).Sessions
	if len(listed) != summaryBatch+1 {
		t.Fatalf("list_sessions listed %d Sessions, want the incarnation's %d", len(listed), summaryBatch+1)
	}
	if listed[0].SessionID != unfolded || listed[1].SessionID != written[len(written)-1] || listed[summaryBatch].SessionID != written[0] {
		t.Errorf("list_sessions listed %s, %s, ..., %s; want the unfolded Session, then the newest, ..., the oldest",
			listed[0].SessionID, listed[1].SessionID, listed[summaryBatch].SessionID)
	}

	f.history.refuse(status.Error(codes.Unavailable, "history or authentication unavailable"))
	if kind := f.attempt(t, f.caller, contract.ActionListSessions, sandbox).failure(); kind != "read_failed" {
		t.Errorf("a refused read failed %q, want read_failed", kind)
	}
}

func TestGetSessionReportsHistorySeparatelyFromDispatcherState(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-command-history")
	session := uuid.NewString()
	summary := summaryOf(f, session, sandbox, nil)
	summary.CommittedCursor = 100
	f.history.hold(summary)
	ids := []string{"test-input-a", "test-input-b", "test-model", "test-effort", "test-failed", "test-noop", "test-admitted", "test-not-ingested"}
	for i, id := range ids {
		cursor := uint64(7 + i*2)
		if i == len(ids)-1 {
			cursor = 150 // The runner/Dispatcher can be ahead of History.
		}
		f.dispatcher.hold(nil, &dispatcherpb.Dispatch{CommandId: id, SessionId: session, State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, Admission: &eventlogpb.EventEntry{Cursor: cursor}})
	}
	confirmation := &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{TurnId: "test-turn", OriginCommandIds: ids[:2]}}}
	var calls atomic.Int32
	f.history.follow = func(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		calls.Add(1)
		kinds := []historypb.EventKind{historypb.EventKind_EVENT_KIND_HARNESS_USER_MESSAGE_CONFIRMED, historypb.EventKind_EVENT_KIND_MODEL_CHANGED, historypb.EventKind_EVENT_KIND_REASONING_EFFORT_CHANGED, historypb.EventKind_EVENT_KIND_COMMAND_FAILED, historypb.EventKind_EVENT_KIND_COMMAND_NOOP}
		if request.GetSessionId() != session || request.GetAfterCursor() != 6 || request.GetFollow() || !slices.Equal(request.GetKinds(), kinds) {
			t.Errorf("command history request = %v", request)
		}
		if _, bounded := stream.Context().Deadline(); !bounded {
			t.Error("command history read has no deadline")
		}
		if err := stream.Send(turnEntries(20, confirmation)); err != nil {
			return err
		}
		if err := stream.Send(turnEntries(
			26,
			confirmation, // Replayed facts neither duplicate dispatches nor promote Dispatcher state.
			&eventpb.Event{Observation: &eventpb.Event_ModelChanged{ModelChanged: &eventpb.ModelChanged{CommandId: ids[2]}}},
			&eventpb.Event{Observation: &eventpb.Event_ReasoningEffortChanged{ReasoningEffortChanged: &eventpb.ReasoningEffortChanged{CommandId: ids[3]}}},
			&eventpb.Event{Observation: &eventpb.Event_CommandFailed{CommandFailed: &eventpb.CommandFailed{CommandId: ids[4], Reason: "test failure"}}},
			&eventpb.Event{Observation: &eventpb.Event_CommandNoop{CommandNoop: &eventpb.CommandNoop{CommandId: ids[5], Reason: "test no-op"}}},
			&eventpb.Event{Observation: &eventpb.Event_ModelChanged{ModelChanged: &eventpb.ModelChanged{CommandId: "test-unlisted"}}},
		)); err != nil {
			return err
		}
		return stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 100, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	}
	state := resultOf[SessionState](f.attempt(t, f.caller, contract.ActionGetSession, turnArguments(t, sandbox, session, nil)))
	want := map[string]*CommandHistory{
		ids[0]: {Outcome: "confirmed", TurnID: new("test-turn")},
		ids[1]: {Outcome: "confirmed", TurnID: new("test-turn")},
		ids[2]: {Outcome: "applied"}, ids[3]: {Outcome: "applied"},
		ids[4]: {Outcome: "failed", Reason: new("test failure")},
		ids[5]: {Outcome: "no_op", Reason: new("test no-op")},
		ids[6]: nil, ids[7]: nil,
	}
	if calls.Load() != 1 || len(state.Dispatches) != len(want) {
		t.Fatalf("follow calls = %d, dispatches = %+v", calls.Load(), state.Dispatches)
	}
	for _, dispatch := range state.Dispatches {
		if dispatch.State != "DELIVERY_STATE_RUNNER_ADMITTED" || !reflect.DeepEqual(dispatch.History, want[dispatch.CommandID]) {
			t.Errorf("dispatch = %+v, history = %+v; want %+v without changing Dispatcher state", dispatch, dispatch.History, want[dispatch.CommandID])
		}
	}
}

func TestGetSessionHistoryStartCoversListedAdmissionsAndIngestLag(t *testing.T) {
	for _, tc := range []struct {
		name       string
		admissions []uint64
		after      uint64
	}{
		{"out of order admissions", []uint64{30, 7}, 6},
		{"unknown admission", []uint64{30, 0}, 0},
		{"all beyond committed prefix", []uint64{150, 170}, 100},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			sandbox := f.launch(t, "test-history-start")
			session := uuid.NewString()
			summary := summaryOf(f, session, sandbox, nil)
			summary.CommittedCursor = 100
			f.history.hold(summary)
			for i, cursor := range tc.admissions {
				f.dispatcher.hold(nil, &dispatcherpb.Dispatch{CommandId: fmt.Sprintf("test-command-%d", i), SessionId: session, Admission: &eventlogpb.EventEntry{Cursor: cursor}})
			}
			f.history.follow = func(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
				if request.GetAfterCursor() != tc.after {
					t.Errorf("start = %d; want %d", request.GetAfterCursor(), tc.after)
				}
				return stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 100, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
			}
			state := resultOf[SessionState](f.attempt(t, f.caller, contract.ActionGetSession, turnArguments(t, sandbox, session, nil)))
			for _, dispatch := range state.Dispatches {
				if dispatch.History != nil {
					t.Errorf("unobserved history = %+v", dispatch.History)
				}
			}
		})
	}
}

func TestGetSessionDoesNotMaskHistoryReadFailure(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-history-failure")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	f.dispatcher.hold(nil, &dispatcherpb.Dispatch{CommandId: "test-command", SessionId: session})
	f.history.follow = func(_ *historypb.FollowSessionRequest, _ grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		return status.Error(codes.DeadlineExceeded, "test History read timeout")
	}
	if kind := f.attempt(t, f.caller, contract.ActionGetSession, turnArguments(t, sandbox, session, nil)).failure(); kind != "read_failed" {
		t.Errorf("read failure = %s", kind)
	}
}

func TestGetSessionBeforeHistoryHasArrived(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-opening-only")
	session := uuid.NewString()
	f.dispatcher.hold(&dispatcherpb.SessionOpening{SessionId: session, SandboxUid: string(sandbox.UID)},
		&dispatcherpb.Dispatch{CommandId: "test-pending-command", SessionId: session, State: dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER})
	state := resultOf[SessionState](f.attempt(t, f.caller, contract.ActionGetSession, turnArguments(t, sandbox, session, nil)))
	if state.Session != nil || len(state.Dispatches) != 1 || state.Dispatches[0].History != nil || state.Dispatches[0].State != "DELIVERY_STATE_DELIVERING_TO_RUNNER" {
		t.Fatalf("opening-only Session = %+v", state)
	}
}
