package projection

import (
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

func TestProjectAttachedSettlesTheLaunchOnItsFirstOutcome(t *testing.T) {
	for name, test := range map[string]struct {
		before   runnerpb.LaunchOutcome
		observed *eventpb.Event
		want     runnerpb.LaunchOutcome
	}{
		"a start decides an undecided launch": {
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_UNSPECIFIED,
			&eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}},
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
		},
		"an exit does not revise a start": {
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
			&eventpb.Event{Observation: &eventpb.Event_HarnessExited{HarnessExited: &eventpb.HarnessExited{}}},
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
		},
		"a failed setup fails a pending launch": {
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
			&eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{ExitCode: 1}}},
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED,
		},
		"a finished setup leaves the launch pending": {
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_UNSPECIFIED,
			&eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{}}},
			runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
		},
	} {
		t.Run(name, func(t *testing.T) {
			attached := &runnerpb.Attached{LaunchOutcome: test.before}
			projectAttached(attached, &eventlogpb.EventEntry{Cursor: 7, Event: test.observed})
			if attached.LaunchOutcome != test.want || attached.LastCursor != 7 {
				t.Fatalf("launch outcome %v at cursor %d, want %v at 7", attached.LaunchOutcome, attached.LastCursor, test.want)
			}
		})
	}
}
