package systemd

import (
	"context"
	"errors"
	"testing"
	"time"
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
	if got.StartState != StartUnconfirmed || got.StartError != "reply lost" || got.Snapshot.LoadState != "not-found" {
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

func TestWaitReturnsRecordedExitBeforeCleanup(t *testing.T) {
	handle := newStartHandle(nil, nil, nil, "native.service", nil, nil, nil)
	unit := UnitSnapshot{LoadState: "loaded", InvocationID: "invocation", ExecMainCode: 1, ExecMainStatus: 23}
	code, observed, err := nativeExitStatus(unit, "invocation")
	if err != nil || !observed || code != 23 {
		t.Fatalf("manager exit = (%d, %t, %v), want (23, true, nil)", code, observed, err)
	}
	handle.recordExit(code, err, false)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if got, err := handle.Wait(ctx); err != nil || got != 23 {
		t.Fatalf("Wait before cleanup = (%d, %v), want (23, nil)", got, err)
	}
	select {
	case <-handle.Done():
		t.Fatal("Wait waited for cleanup or cleanup completed before the exit result was returned")
	default:
	}
	handle.completeWith(code, nil)
	if err := handle.waitFenced(ctx); err != nil {
		t.Fatalf("fenced wait after cleanup: %v", err)
	}
}
