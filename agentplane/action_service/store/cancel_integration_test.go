package store

import (
	"errors"
	"reflect"
	"slices"
	"testing"

	"github.com/google/uuid"
)

func (f fixture) cancel(t *testing.T, id uuid.UUID) CancellationResult {
	t.Helper()
	result, err := f.Cancel(t.Context(), id, caller)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func TestAPendingCancellationIsDurableAndIdempotent(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "cancelled")

	result := f.cancel(t, pending.ID)
	if result.Outcome != OutcomeCancelled || result.Request.State != StateCancelled || result.Request.Decision != nil ||
		result.Request.Execution != nil || result.Request.Version != pending.Version+1 {
		t.Errorf("result %+v", result)
	}

	// A Store over the same database is a restarted service.
	restarted := New(f.db, f.grants)
	duplicate, err := restarted.Cancel(t.Context(), pending.ID, caller)
	if err != nil || duplicate.Outcome != OutcomeAlreadyCancelled || !reflect.DeepEqual(duplicate.Request, result.Request) {
		t.Errorf("duplicate = %+v, %v", duplicate, err)
	}
	events := f.events(t, pending.ID)
	if !slices.Equal(eventStates(events), []ActionState{StateDecisionPending, StateCancelled}) ||
		events[1].Actor != caller || !events[1].At.Equal(result.Request.UpdatedAt) {
		t.Errorf("events %+v", events)
	}

	// Neither the version the operator saw nor a refreshed one authorizes a late approval.
	if _, _, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow, "test-decision"), operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("approval of the version the operator saw: %v", err)
	}
	if _, _, err := f.Decide(t.Context(), pending.ID, decision(result.Request, VerdictAllow, "test-decision"), operator); conflictReason(err) != ConflictAlreadyDecided {
		t.Errorf("approval of the refreshed version: %v", err)
	}
}

func TestAnAllowedCancellationKeepsItsDecisionAndStopsDispatch(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "allowed")
	allowed := f.decide(t, pending, VerdictAllow)

	result := f.cancel(t, allowed.ID)
	execution := result.Request.Execution
	if result.Outcome != OutcomeCancelled || !reflect.DeepEqual(result.Request.Decision, allowed.Decision) || execution == nil ||
		execution.State != ExecutionCancelled || execution.StartedAt != nil || execution.Result != nil || execution.Error != nil ||
		execution.CompletedAt == nil || !execution.CompletedAt.Equal(result.Request.UpdatedAt) {
		t.Errorf("result %+v, execution %+v", result, execution)
	}
	if f.count(t, `SELECT count(*) FROM action_execution WHERE state = 'pending_dispatch'`) != 0 {
		t.Error("the cancelled request still awaits dispatch")
	}

	replay, dispatch, err := f.Decide(t.Context(), allowed.ID, decision(pending, VerdictAllow, "decision-allowed"), operator)
	if err != nil || dispatch || replay.State != StateCancelled {
		t.Errorf("replayed approval = %v, dispatch %v, %v", replay.State, dispatch, err)
	}
}

func TestOnlyTheOwnerCanCancel(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "owned")

	for name, principal := range map[string]Principal{"another caller": otherCaller, "an operator": operator} {
		if _, err := f.Cancel(t.Context(), pending.ID, principal); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := f.Cancel(t.Context(), uuid.New(), caller); !errors.Is(err, ErrNotFound) {
		t.Errorf("an unknown request: %v", err)
	}
	if f.get(t, pending.ID).State != StateDecisionPending {
		t.Fatal("a cancellation by a non-owner took effect")
	}
	f.cancel(t, pending.ID)
	// A request that is already cancelled does not tell a non-owner so.
	if _, err := f.Cancel(t.Context(), pending.ID, operator); !errors.Is(err, ErrNotFound) {
		t.Errorf("an operator after the owner cancelled: %v", err)
	}
}

func TestCancellationOutcomeFollowsTheRequestsState(t *testing.T) {
	for _, test := range []struct {
		state     ActionState
		execution ExecutionState
		want      CancellationOutcome
	}{
		{StateDispatching, ExecutionDispatching, OutcomeTooLate},
		{StateRunning, ExecutionRunning, OutcomeTooLate},
		{StateExecutionUnknown, ExecutionUnknown, OutcomeTooLate},
		{StateSucceeded, ExecutionSucceeded, OutcomeAlreadyFinished},
		{StateFailed, ExecutionFailed, OutcomeAlreadyFinished},
	} {
		t.Run(string(test.state), func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "claimed")
			f.exec(t, `UPDATE action_request SET state = $2 WHERE id = $1`, allowed.ID, test.state)
			f.exec(t, `UPDATE action_execution SET state = $2 WHERE request_id = $1`, allowed.ID, test.execution)
			before, events := f.get(t, allowed.ID), f.events(t, allowed.ID)

			result := f.cancel(t, allowed.ID)
			if result.Outcome != test.want || !reflect.DeepEqual(result.Request, before) || !reflect.DeepEqual(f.events(t, allowed.ID), events) {
				t.Errorf("outcome %s, request changed %v", result.Outcome, !reflect.DeepEqual(result.Request, before))
			}
		})
	}
}

func TestADeniedRequestIsAlreadyFinished(t *testing.T) {
	f := newFixture(t)
	denied := f.decide(t, f.submit(t, caller, "denied"), VerdictDeny)

	result := f.cancel(t, denied.ID)
	if result.Outcome != OutcomeAlreadyFinished || !reflect.DeepEqual(result.Request, denied) {
		t.Errorf("result %+v", result)
	}
}

func TestAnAllowedRequestWithoutAPendingDispatchCannotBeCancelled(t *testing.T) {
	for name, strand := range map[string]func(t *testing.T, f fixture, id uuid.UUID){
		"claimed execution": func(t *testing.T, f fixture, id uuid.UUID) {
			f.exec(t, `UPDATE action_execution SET state = 'dispatching' WHERE request_id = $1`, id)
		},
		"no execution": func(t *testing.T, f fixture, id uuid.UUID) {
			f.exec(t, `DELETE FROM action_execution WHERE request_id = $1`, id)
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "stranded")
			strand(t, f, allowed.ID)
			before := f.get(t, allowed.ID)

			if _, err := f.Cancel(t.Context(), allowed.ID, caller); conflictReason(err) != ConflictNoPendingDispatch {
				t.Errorf("got %v", err)
			}
			if !reflect.DeepEqual(f.get(t, allowed.ID), before) {
				t.Error("a refused cancellation changed the request")
			}
		})
	}
}
