package orchestrationv1

import (
	"fmt"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var testEpoch = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

// summaryOf is a summary of a Session that sandbox wrote, first written at first.
func summaryOf(f fixture, sessionID string, sandbox SandboxRef, first *time.Time) *historypb.SessionSummary {
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
	for range recentDispatches + 2 {
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

	state := resultOf[SessionState](f.attempt(t, f.caller, actionGetSession,
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
	if len(state.Dispatches) != recentDispatches || state.Dispatches[0].CommandID != commands[len(commands)-1] ||
		state.Dispatches[recentDispatches-1].CommandID != commands[2] {
		t.Errorf("dispatches = %+v, want the %d most recent, newest first", state.Dispatches, recentDispatches)
	}
}

// A grant on one incarnation reads only its Sessions, whichever of the Session Dispatcher and the History Service knows
// the Session.
func TestGetSessionRefusesASessionItCannotTieToTheIncarnation(t *testing.T) {
	f := newFixture(t)
	launched := f.launch(t, "test-worker")
	other := SandboxRef{Name: launched.Name, UID: types.UID(uuid.NewString())}
	for name, test := range map[string]struct {
		opened, written *SandboxRef
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
			attempt := f.attempt(t, f.caller, actionGetSession,
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
		summaryOf(f, uuid.NewString(), SandboxRef{Name: launched.Name, UID: types.UID(uuid.NewString())}, &testEpoch),
	)

	sandbox := fmt.Sprintf(`{"sandbox": {"name": %q, "uid": %q}}`, launched.Name, launched.UID)
	listed := resultOf[SessionList](f.attempt(t, f.caller, actionListSessions, sandbox)).Sessions
	if len(listed) != summaryBatch+1 {
		t.Fatalf("list_sessions listed %d Sessions, want the incarnation's %d", len(listed), summaryBatch+1)
	}
	if listed[0].SessionID != unfolded || listed[1].SessionID != written[len(written)-1] || listed[summaryBatch].SessionID != written[0] {
		t.Errorf("list_sessions listed %s, %s, ..., %s; want the unfolded Session, then the newest, ..., the oldest",
			listed[0].SessionID, listed[1].SessionID, listed[summaryBatch].SessionID)
	}

	f.history.refuse(status.Error(codes.Unavailable, "history or authentication unavailable"))
	if kind := f.attempt(t, f.caller, actionListSessions, sandbox).failure(); kind != "read_failed" {
		t.Errorf("a refused read failed %q, want read_failed", kind)
	}
}
