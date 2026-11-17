package systemd

import (
	"errors"
	"testing"
)

func TestObservationReplayAndDeduplication(t *testing.T) {
	handle := &StartHandle{name: "native.service"}
	unit := UnitSnapshot{InvocationID: "invocation", ActiveState: "active"}
	handle.invocationID, handle.group = "invocation", "/test"
	handle.observeUnit(unit, nil)
	var got []Observation
	handle.Observe(func(observation Observation) { got = append(got, observation) })
	handle.observeUnit(unit, nil)
	handle.observeUnit(UnitSnapshot{}, errors.New("disconnected"))
	if len(got) != 2 || got[0].Revision != 1 || got[0].Snapshot != unit || got[1].Revision != 2 || got[1].Error != "disconnected" || got[1].Snapshot != unit || got[1].ExpectedInvocationID != "invocation" || got[1].CapturedControlGroup != "/test" {
		t.Fatalf("observations = %+v", got)
	}
}

func TestUnconfirmedStartSurvivesLaterManagerObservations(t *testing.T) {
	handle := &StartHandle{name: "native.service"}
	handle.observeStart(StartSubmitted, nil)
	handle.observeStart(StartUnconfirmed, errors.New("reply lost"))
	handle.observeUnit(UnitSnapshot{LoadState: "not-found"}, nil)
	var got Observation
	handle.Observe(func(observation Observation) { got = observation })
	if handle.Accepted() || !handle.Unconfirmed() || got.StartState != StartUnconfirmed || got.StartError != "reply lost" || got.Snapshot.LoadState != "not-found" {
		t.Fatalf("unconfirmed start observation = %+v", got)
	}
}

func TestExitOutcomeSurvivesUnitCollection(t *testing.T) {
	for _, test := range []struct {
		name     string
		observed bool
		code     int
		err      error
		want     ExitState
	}{
		{name: "observed", observed: true, code: 23, want: ExitObserved},
		{name: "unknown", code: -1, err: errors.New("exit unavailable"), want: ExitUnknown},
	} {
		t.Run(test.name, func(t *testing.T) {
			handle := &StartHandle{name: "native.service"}
			handle.invocationID, handle.group = "invocation", "/test"
			handle.observeUnit(UnitSnapshot{InvocationID: "invocation"}, nil)
			handle.observeExit(test.code, test.observed, test.err)
			handle.observeUnit(UnitSnapshot{LoadState: "not-found"}, nil)
			var got Observation
			handle.Observe(func(observation Observation) { got = observation })
			if got.ExitState != test.want || got.ExitCode != test.code || got.Snapshot.LoadState != "not-found" {
				t.Fatalf("exit observation = %+v", got)
			}
			if test.err != nil && got.ExitError != test.err.Error() {
				t.Fatalf("exit observation error = %q, want %q", got.ExitError, test.err)
			}
		})
	}
}
