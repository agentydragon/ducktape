package projection

import (
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestProjectAttachedSettlesTheLaunchOnItsFirstOutcome(t *testing.T) {
	for name, test := range map[string]struct {
		before   sessionpb.LaunchOutcome
		observed *eventpb.Event
		want     sessionpb.LaunchOutcome
	}{
		"a start decides an undecided launch": {
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_UNSPECIFIED,
			&eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}},
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
		},
		"an exit does not revise a start": {
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
			&eventpb.Event{Observation: &eventpb.Event_HarnessExited{HarnessExited: &eventpb.HarnessExited{}}},
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED,
		},
		"a failed setup fails a pending launch": {
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
			&eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{ExitCode: 1}}},
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED,
		},
		"a finished setup leaves the launch pending": {
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_UNSPECIFIED,
			&eventpb.Event{Observation: &eventpb.Event_SetupFinished{SetupFinished: &eventpb.SetupFinished{}}},
			sessionpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING,
		},
	} {
		t.Run(name, func(t *testing.T) {
			attached := &sessionpb.Attached{LaunchOutcome: test.before}
			projectAttached(attached, &eventlogpb.EventEntry{Cursor: 7, Event: test.observed})
			if attached.LaunchOutcome != test.want || attached.LastCursor != 7 {
				t.Fatalf("launch outcome %v at cursor %d, want %v at 7", attached.LaunchOutcome, attached.LastCursor, test.want)
			}
		})
	}
}
