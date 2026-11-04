package supervision

import (
	"errors"
	"testing"
	"time"
)

func TestSystemdObservationReplayAndDeduplication(t *testing.T) {
	p := &Process{}
	unit := SystemdUnitSnapshot{InvocationID: "invocation", ActiveState: "active"}
	p.observeSystemd("native.service", "invocation", "/test", unit, nil)
	var got []SystemdObservation
	p.ObserveSystemd(func(observation SystemdObservation) { got = append(got, observation) })
	p.observeSystemd("native.service", "invocation", "/test", unit, nil)
	p.observeSystemd("native.service", "invocation", "/test", SystemdUnitSnapshot{}, errors.New("disconnected"))
	if len(got) != 2 || got[0].Revision != 1 || got[0].Snapshot != unit || got[1].Revision != 2 || got[1].Error != "disconnected" || got[1].Snapshot != unit || got[1].ExpectedInvocationID != "invocation" || got[1].CapturedControlGroup != "/test" {
		t.Fatalf("observations = %+v", got)
	}
}

func TestSystemdUnconfirmedStartSurvivesLaterManagerObservations(t *testing.T) {
	p := &Process{}
	p.observeSystemdStart("native.service", SystemdStartSubmitted, nil)
	p.observeSystemdStart("native.service", SystemdStartUnconfirmed, errors.New("reply lost"))
	p.observeSystemd("native.service", "", "", SystemdUnitSnapshot{LoadState: "not-found"}, nil)
	var got SystemdObservation
	p.ObserveSystemd(func(observation SystemdObservation) { got = observation })
	if got.StartState != SystemdStartUnconfirmed || got.StartError != "reply lost" || got.Snapshot.LoadState != "not-found" {
		t.Fatalf("unconfirmed start observation = %+v", got)
	}
}

func TestSystemdExitOutcomeSurvivesUnitCollection(t *testing.T) {
	for _, test := range []struct {
		name     string
		observed bool
		code     int
		err      error
		want     SystemdExitState
	}{
		{name: "observed", observed: true, code: 23, want: SystemdExitObserved},
		{name: "unknown", code: -1, err: errors.New("exit unavailable"), want: SystemdExitUnknown},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := &Process{}
			p.observeSystemd("native.service", "invocation", "/test", SystemdUnitSnapshot{InvocationID: "invocation"}, nil)
			p.observeSystemdExit(test.code, test.observed, test.err)
			p.observeSystemd("native.service", "invocation", "/test", SystemdUnitSnapshot{LoadState: "not-found"}, nil)
			var got SystemdObservation
			p.ObserveSystemd(func(observation SystemdObservation) { got = observation })
			if got.ExitState != test.want || got.ExitCode != test.code || got.Snapshot.LoadState != "not-found" {
				t.Fatalf("exit observation = %+v", got)
			}
			if test.err != nil && got.ExitError != test.err.Error() {
				t.Fatalf("exit observation error = %q, want %q", got.ExitError, test.err)
			}
		})
	}
}

func TestNewProcessRejectsNegativeStopGrace(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command:   []string{"/bin/true"},
		StopGrace: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative stop grace")
	}
}

func TestNewProcessRejectsNegativeFenceTimeout(t *testing.T) {
	_, err := (&Supervisor{}).NewProcess(ProcessConfig{
		Command: []string{"/bin/true"}, FenceTimeout: -time.Nanosecond,
	})
	if err == nil {
		t.Fatal("NewProcess accepted a negative fence timeout")
	}
}
