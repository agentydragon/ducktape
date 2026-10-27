package orchestrationv1

import (
	"cmp"
	"context"
	"slices"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// recentDispatches is how many of a Session's Dispatches get_session answers with, newest first.
const recentDispatches = 20

// summaryBatch is the most Sessions one BatchGetSessionSummaries reads.
const summaryBatch = 200

type getSessionArgs struct {
	Sandbox   SandboxRef `json:"sandbox"`
	SessionID string     `json:"session_id"`
}

type listSessionsArgs struct {
	Sandbox SandboxRef `json:"sandbox"`
}

// SessionListing is a Session as its history records it, in the fields a listing of a Sandbox's Sessions shows: none
// of its instructions, working directory or transcript.
type SessionListing struct {
	SessionID string `json:"session_id"`
	// Runner is absent until a runner has attached the Session.
	Runner *RunnerSnapshot `json:"runner,omitempty"`
	// FeedStatus is FEED_STATUS_UNSPECIFIED until a runner attachment is observed.
	FeedStatus     string     `json:"feed_status"`
	FeedError      *string    `json:"feed_error,omitempty"`
	IdleSince      *time.Time `json:"idle_since,omitempty"`
	LastTurnStatus *string    `json:"last_turn_status,omitempty"`
	FirstEventAt   *time.Time `json:"first_event_at,omitempty"`
	LastEventAt    *time.Time `json:"last_event_at,omitempty"`
}

// RunnerSnapshot is the runner's last report of the Session.
type RunnerSnapshot struct {
	Harness       string `json:"harness"`
	HarnessState  string `json:"harness_state"`
	LaunchOutcome string `json:"launch_outcome"`
	// ActiveTurnID is absent while no turn runs.
	ActiveTurnID *string `json:"active_turn_id,omitempty"`
}

// SessionState is the result of get_session.
type SessionState struct {
	// SandboxReady is the Sandbox's Ready condition, absent until its controller sets it.
	SandboxReady *Readiness `json:"sandbox_ready,omitempty"`
	// Opening is absent for a Session not opened through the Session Dispatcher.
	Opening *Delivery `json:"opening,omitempty"`
	// Session is absent until the Session's runner has written it.
	Session    *SessionListing   `json:"session,omitempty"`
	Dispatches []CommandDelivery `json:"dispatches"`
}

// Readiness is a Sandbox's Ready condition.
type Readiness struct {
	Status  metav1.ConditionStatus `json:"status"`
	Reason  string                 `json:"reason"`
	Message string                 `json:"message,omitempty"`
}

// Delivery is where the Session Dispatcher has a Session's opening or one of its commands.
type Delivery struct {
	State     string    `json:"state"`
	Reason    *string   `json:"reason,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// CommandDelivery is where the Session Dispatcher has one of a Session's commands.
type CommandDelivery struct {
	CommandID string `json:"command_id"`
	Operation string `json:"operation"`
	Delivery
}

// SessionList is the result of list_sessions.
type SessionList struct {
	Sessions []SessionListing `json:"sessions"`
}

// getSession reads a Session of the named incarnation from the API server, the Session Dispatcher and the History
// Service. A Session that neither its opening nor its history ties to that incarnation is refused as not found, so a
// grant on one Sandbox reads no other's Sessions.
func (e *Executor) getSession(ctx context.Context, args getSessionArgs) (SessionState, error) {
	sandbox, err := e.requireSandbox(ctx, args.Sandbox)
	if err != nil {
		return SessionState{}, err
	}
	opening, err := e.dispatcher.GetSessionOpening(ctx, &dispatcherpb.GetSessionOpeningRequest{SessionId: args.SessionID})
	if err != nil && status.Code(err) != codes.NotFound {
		return SessionState{}, readError("Session Dispatcher", err)
	}
	summary, err := e.history.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: args.SessionID})
	if err != nil && status.Code(err) != codes.NotFound {
		return SessionState{}, readError("History Service", err)
	}
	// The incarnations the Session's opening and its history tie it to, as far as each is known.
	var tied []string
	if opening != nil {
		tied = append(tied, opening.GetOpening().GetSandboxUid())
	}
	if written := summary.GetSandbox(); written != nil && written.Uid != nil {
		tied = append(tied, *written.Uid)
	}
	if len(tied) == 0 || slices.ContainsFunc(tied, func(uid string) bool { return uid != string(args.Sandbox.UID) }) {
		return SessionState{}, refuse("session_not_found", "no Session %s of Sandbox %q incarnation %s is known",
			args.SessionID, args.Sandbox.Name, args.Sandbox.UID)
	}
	listed, err := e.dispatcher.ListDispatches(ctx, &dispatcherpb.ListDispatchesRequest{
		Filter: &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: args.SessionID}},
	})
	if err != nil {
		return SessionState{}, readError("Session Dispatcher", err)
	}
	dispatches := listed.GetDispatches()
	recent := dispatches[max(0, len(dispatches)-recentDispatches):]
	state := SessionState{Dispatches: make([]CommandDelivery, 0, len(recent))}
	if ready := meta.FindStatusCondition(sandbox.Status.Conditions, sandboxapi.ConditionReady); ready != nil {
		state.SandboxReady = &Readiness{Status: ready.Status, Reason: ready.Reason, Message: ready.Message}
	}
	if o := opening.GetOpening(); o != nil {
		state.Opening = &Delivery{
			State: o.GetState().String(), Reason: o.Reason, CreatedAt: o.GetCreatedAt().AsTime(), UpdatedAt: o.GetUpdatedAt().AsTime(),
		}
	}
	if summary != nil {
		state.Session = new(listing(summary))
	}
	for _, dispatch := range slices.Backward(recent) {
		state.Dispatches = append(state.Dispatches, CommandDelivery{
			CommandID: dispatch.GetCommandId(), Operation: dispatch.GetOperation().String(),
			Delivery: Delivery{
				State: dispatch.GetState().String(), Reason: dispatch.Reason,
				CreatedAt: dispatch.GetCreatedAt().AsTime(), UpdatedAt: dispatch.GetUpdatedAt().AsTime(),
			},
		})
	}
	return state, nil
}

// listSessions reads the Sessions the History Service records for the named incarnation, which it finds by UID alone:
// a Sandbox deleted since keeps its Sessions there.
func (e *Executor) listSessions(ctx context.Context, args listSessionsArgs) (SessionList, error) {
	found, err := e.history.FindSessions(ctx, &historypb.FindSessionsRequest{
		Sandbox: &historypb.FindSessionsRequest_SandboxUid{SandboxUid: string(args.Sandbox.UID)},
	})
	if err != nil {
		return SessionList{}, readError("History Service", err)
	}
	ids := make([]string, 0, len(found.GetSessions()))
	for _, session := range found.GetSessions() {
		ids = append(ids, session.GetSessionId())
	}
	list := SessionList{Sessions: make([]SessionListing, 0, len(ids))}
	for batch := range slices.Chunk(ids, summaryBatch) {
		read, err := e.history.BatchGetSessionSummaries(ctx, &historypb.BatchGetSessionSummariesRequest{SessionIds: batch})
		if err != nil {
			return SessionList{}, readError("History Service", err)
		}
		for _, summary := range read.GetSummaries() {
			list.Sessions = append(list.Sessions, listing(summary))
		}
	}
	// Newest first by first event, a Session with none folded yet ahead of the rest.
	slices.SortFunc(list.Sessions, func(a, b SessionListing) int {
		switch {
		case a.FirstEventAt == nil && b.FirstEventAt != nil:
			return -1
		case a.FirstEventAt != nil && b.FirstEventAt == nil:
			return 1
		case a.FirstEventAt != nil && !a.FirstEventAt.Equal(*b.FirstEventAt):
			return b.FirstEventAt.Compare(*a.FirstEventAt)
		}
		return cmp.Compare(a.SessionID, b.SessionID)
	})
	return list, nil
}

// listing is summary in the listing's fields.
func listing(summary *historypb.SessionSummary) SessionListing {
	l := SessionListing{
		SessionID: summary.GetSessionId(), FeedStatus: summary.GetFeedStatus().String(), FeedError: summary.FeedError,
		IdleSince: timeOf(summary.GetIdleSince()), FirstEventAt: timeOf(summary.GetFirstEventAt()),
		LastEventAt: timeOf(summary.GetLastEventAt()),
	}
	if summary.LastTurnStatus != nil {
		l.LastTurnStatus = new(summary.GetLastTurnStatus().String())
	}
	if attached := summary.GetAttached(); attached != nil {
		l.Runner = &RunnerSnapshot{
			Harness: attached.GetSpec().GetHarness().String(), HarnessState: attached.GetHarnessState().String(),
			LaunchOutcome: attached.GetLaunchOutcome().String(),
		}
		if turn := attached.GetActiveTurnId(); turn != "" {
			l.Runner.ActiveTurnID = &turn
		}
	}
	return l
}

func timeOf(timestamp *timestamppb.Timestamp) *time.Time {
	if timestamp == nil {
		return nil
	}
	return new(timestamp.AsTime())
}

// readError is a failed read as the attempt's outcome. A read has no effect, so its outcome is never unknown: it
// failed, and the caller may ask again.
func readError(service string, err error) error {
	return refuse("read_failed", "reading the %s: %v", service, err)
}
