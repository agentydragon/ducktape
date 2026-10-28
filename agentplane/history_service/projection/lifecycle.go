package projection

import (
	"context"
	"database/sql"
	"encoding/json"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// projectLifecycle folds the Session's service lifecycle, only within the fold's materialized prefix: the
// attachment snapshot the page carries, the Events after it, and whether the stream has ended. It updates
// summary in place and reports whether the summary or the operational state changed.
func projectLifecycle(
	ctx context.Context, tx *sql.Tx, threadID uuid.UUID, summary *summary,
	page *historypb.ReadSessionEventsResponse, throughCursor uint64,
) (bool, error) {
	previous := *summary
	var attached *runnerpb.Attached
	if summary.attached != nil {
		attached = &runnerpb.Attached{}
		if err := protojson.Unmarshal(summary.attached, attached); err != nil {
			return false, err
		}
	}
	// end is nil for a stream that has not ended, empty for one that ended cleanly, and otherwise the failure
	// that ended it.
	var end map[string]string
	if summary.end != nil {
		if err := json.Unmarshal(summary.end, &end); err != nil {
			return false, err
		}
	}
	if feed := page.GetFeedState(); feed != nil {
		if feed.GetAttached() == nil || feed.GetAttached().GetLastCursor() > page.GetLastCursor() {
			return false, replication(nil, "stored feed state is outside the committed prefix")
		}
		cursor := feed.GetAttached().GetLastCursor()
		// A page may carry a feed snapshot past the entries folded so far. Wait until the fold covers it; never
		// leap the view over unfolded Events.
		if cursor <= throughCursor && (attached == nil || cursor >= attached.GetLastCursor()) {
			if attached == nil || cursor != attached.GetLastCursor() {
				end = nil
			}
			attached = proto.Clone(feed.GetAttached()).(*runnerpb.Attached)
		}
	}
	if attached == nil {
		// Successful projection clears an earlier projection error even if imported history has no runner
		// lifecycle snapshot. This does not manufacture EOF.
		return SetOperational(ctx, tx, threadID, threadfold.StatusActive, nil, nil)
	}
	for _, entry := range page.GetEntries() {
		if entry.GetCursor() <= attached.GetLastCursor() {
			continue
		}
		projectAttached(attached, entry)
		// Any suffix invalidates a failure at the old prefix, including failed setup/restart.
		end = nil
	}
	if len(end) == 0 {
		// An admitted input owns work while its harness starts. Process absence alone cannot end the stream
		// before that input's startup/delivery outcome arrives.
		var pendingCommand bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM thread_entity entity
				JOIN thread_checkpoint checkpoint ON checkpoint.thread_id = entity.thread_id
					AND checkpoint.projection_epoch = entity.projection_epoch
				WHERE entity.thread_id = $1 AND entity.entity_kind = $2 AND entity.pending)`,
			threadID, threadfold.KindCommand).Scan(&pendingCommand); err != nil {
			return false, err
		}
		end = nil
		if attached.GetHarnessState() == runnerpb.HarnessState_HARNESS_STATE_STOPPED &&
			attached.GetSetupState() != runnerpb.SetupState_SETUP_STATE_RUNNING &&
			attached.GetLaunchOutcome() != runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING && !pendingCommand {
			end = map[string]string{}
		}
	}
	var err error
	if summary.attached, err = protojson.Marshal(attached); err != nil {
		return false, err
	}
	if summary.end, err = marshalEnd(end); err != nil {
		return false, err
	}

	status, message := threadfold.StatusActive, (*string)(nil)
	switch {
	case len(end) > 0:
		status = threadfold.StatusFailed
		if text, ok := end["message"]; ok {
			message = &text
		}
	case end != nil:
		status = threadfold.StatusEnded
	}
	operationalChanged, err := SetOperational(ctx, tx, threadID, status, message, nil)
	if err != nil {
		return false, err
	}
	same, err := summary.sameAs(&previous)
	return !same || operationalChanged, err
}

func marshalEnd(end map[string]string) (json.RawMessage, error) {
	if end == nil {
		return nil, nil
	}
	return json.Marshal(end)
}

// projectAttached advances an attachment snapshot over one Event.
func projectAttached(attached *runnerpb.Attached, entry *eventlogpb.EventEntry) {
	attached.LastCursor = entry.GetCursor()
	event := entry.GetEvent()
	// The runner's own rule (runner/session/events.go): the first outcome settles the launch. An announcement from a
	// runner that predates the field leaves it unspecified until then.
	undecided := attached.GetLaunchOutcome() == runnerpb.LaunchOutcome_LAUNCH_OUTCOME_UNSPECIFIED ||
		attached.GetLaunchOutcome() == runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING
	decide := func(outcome runnerpb.LaunchOutcome) {
		if undecided {
			attached.LaunchOutcome = outcome
		}
	}
	switch observation := event.GetObservation().(type) {
	case *eventpb.Event_SetupFinished:
		if observation.SetupFinished.GetExitCode() == 0 {
			attached.SetupState = runnerpb.SetupState_SETUP_STATE_SUCCEEDED
			decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING)
		} else {
			attached.SetupState = runnerpb.SetupState_SETUP_STATE_FAILED
			decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED)
		}
	case *eventpb.Event_SetupInterrupted:
		attached.SetupState = runnerpb.SetupState_SETUP_STATE_INTERRUPTED
		decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED)
	case *eventpb.Event_HarnessLaunchFailed:
		decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED)
	case *eventpb.Event_HarnessStarted:
		attached.HarnessState = runnerpb.HarnessState_HARNESS_STATE_RUNNING
		decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED)
	case *eventpb.Event_HarnessExited, *eventpb.Event_HarnessLost:
		attached.HarnessState = runnerpb.HarnessState_HARNESS_STATE_STOPPED
		attached.ActiveTurnId = ""
		decide(runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED)
	case *eventpb.Event_TurnStarted:
		attached.ActiveTurnId = observation.TurnStarted.GetTurnId()
	case *eventpb.Event_TurnCompleted:
		attached.ActiveTurnId = ""
	case *eventpb.Event_ModelChanged:
		specOf(attached).Model = observation.ModelChanged.GetModel()
	case *eventpb.Event_ReasoningEffortChanged:
		specOf(attached).ReasoningEffort = observation.ReasoningEffortChanged.GetEffort()
	}
}

func specOf(attached *runnerpb.Attached) *runnerpb.SessionSpec {
	if attached.Spec == nil {
		attached.Spec = &runnerpb.SessionSpec{}
	}
	return attached.Spec
}
