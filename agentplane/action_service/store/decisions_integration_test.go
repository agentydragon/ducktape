package store

import (
	"errors"
	"reflect"
	"slices"
	"testing"

	"github.com/google/uuid"
)

func decision(request ActionRequest, verdict Verdict) DecisionInput {
	return DecisionInput{Verdict: verdict, ExpectedVersion: request.Version}
}

func TestDecideRecordsTheOperatorsVerdict(t *testing.T) {
	note := "test note"
	for _, test := range []struct {
		verdict Verdict
		state   ActionState
	}{
		{VerdictAllow, StateAllowed},
		{VerdictDeny, StateDenied},
	} {
		t.Run(string(test.verdict), func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "decided")
			input := decision(pending, test.verdict)
			input.DecisionNote = &note

			got, err := f.Decide(t.Context(), pending.ID, input, operator)
			if err != nil {
				t.Fatal(err)
			}
			if got.State != test.state || got.Version != 2 {
				t.Errorf("state %s, version %d", got.State, got.Version)
			}
			decided := got.Decision
			if decided == nil || decided.Verdict != test.verdict || *decided.Operator != operator || *decided.DecisionNote != note ||
				decided.ReasonDescription != nil || decided.PolicyEvidence != nil || !decided.DecidedAt.Equal(got.UpdatedAt) {
				t.Errorf("decision %+v", decided)
			}
			// Python reads the provider and reason code Go no longer does.
			if f.count(t, `SELECT count(*) FROM action_decision WHERE provider = 'human_operator' AND reason_code IS NULL`) != 1 {
				t.Error("the decision is not recorded as Python records an operator's")
			}
			awaitsDispatch := test.verdict == VerdictAllow
			if (got.Execution != nil) != awaitsDispatch {
				t.Fatalf("execution = %+v, want one: %v", got.Execution, awaitsDispatch)
			}
			if awaitsDispatch && (got.Execution.State != ExecutionPendingDispatch || !got.Execution.CreatedAt.Equal(got.UpdatedAt)) {
				t.Errorf("execution %+v", got.Execution)
			}
			if !reflect.DeepEqual(f.get(t, pending.ID), got) {
				t.Errorf("read back %+v, want %+v", f.get(t, pending.ID), got)
			}
			events := f.events(t, pending.ID)
			if !slices.Equal(eventStates(events), []ActionState{StateDecisionPending, test.state}) || events[1].Actor != nil {
				t.Errorf("events %+v", events)
			}
		})
	}
}

func TestARepeatedDecisionFindsTheVersionItNamedGone(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "repeated")
	first, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow), operator)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow), operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("the repeated decision: %v", err)
	}
	if !reflect.DeepEqual(f.get(t, pending.ID), first) || f.count(t, `SELECT count(*) FROM action_execution`) != 1 {
		t.Error("the repeated decision wrote")
	}

	// The same operator's Decisions on other requests are their own.
	for _, key := range []string{"second", "third"} {
		if _, err := f.Decide(t.Context(), f.submit(t, caller, key).ID, DecisionInput{Verdict: VerdictDeny, ExpectedVersion: 1}, operator); err != nil {
			t.Errorf("%s: %v", key, err)
		}
	}
}

func TestADecisionNeedsTheCurrentVersionOfAPendingRequest(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "versioned")
	stale := decision(pending, VerdictAllow)
	stale.ExpectedVersion = pending.Version + 1
	if _, err := f.Decide(t.Context(), pending.ID, stale, operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("a version the request has not reached: %v", err)
	}
	if !reflect.DeepEqual(f.get(t, pending.ID), pending) || f.count(t, `SELECT count(*) FROM action_decision`) != 0 {
		t.Error("a refused decision wrote")
	}

	decided := f.decide(t, pending, VerdictAllow)
	// The version is checked first: an operator who saw an older request learns it changed.
	if _, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictDeny), operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("an older version of a decided request: %v", err)
	}
	if _, err := f.Decide(t.Context(), pending.ID, decision(decided, VerdictDeny), operator); conflictReason(err) != ConflictAlreadyDecided {
		t.Errorf("the current version of a decided request: %v", err)
	}
}

func TestADecidedRequestRefusesAnotherDecision(t *testing.T) {
	f := newFixture(t)
	cancelled := f.submit(t, caller, "cancelled")
	if _, err := f.Cancel(t.Context(), cancelled.ID, caller); err != nil {
		t.Fatal(err)
	}
	for name, request := range map[string]ActionRequest{
		"operator allowed": f.allowed(t, caller, "operator-allowed"),
		"operator denied":  f.decide(t, f.submit(t, caller, "operator-denied"), VerdictDeny),
		"policies allowed": f.submitInput(t, caller, input("policies-allowed"), Admission{RequestID: uuid.New(), Vote: &ProviderVote{}}),
		"cancelled":        f.get(t, cancelled.ID),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := f.Decide(t.Context(), request.ID, decision(request, VerdictDeny), operator)
			if conflictReason(err) != ConflictAlreadyDecided {
				t.Errorf("got %v", err)
			}
			if !reflect.DeepEqual(f.get(t, request.ID), request) {
				t.Error("a refused decision changed the request")
			}
		})
	}
}

func TestDecideRefusesWhatItCannotRecord(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "refused")
	valid := decision(pending, VerdictAllow)

	if _, err := f.Decide(t.Context(), uuid.New(), valid, operator); !errors.Is(err, ErrNotFound) {
		t.Errorf("an unknown request: %v", err)
	}
	for name, test := range map[string]struct {
		input    DecisionInput
		operator OperatorPrincipal
	}{
		"verdict":          {DecisionInput{Verdict: "maybe", ExpectedVersion: 1}, operator},
		"version below 1":  {DecisionInput{Verdict: VerdictAllow}, operator},
		"anonymous issuer": {valid, OperatorPrincipal{Subject: "test-operator"}},
	} {
		if _, err := f.Decide(t.Context(), pending.ID, test.input, test.operator); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if f.get(t, pending.ID).State != StateDecisionPending {
		t.Error("a refused decision changed the request")
	}
}

func TestConcurrentIdenticalDecisionsRecordOne(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "twice")
	release := f.holdRequest(t, pending.ID)

	decide := func() <-chan decided {
		return async(func() decided {
			request, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow), operator)
			return decided{request, err}
		})
	}
	first, second := decide(), decide()
	f.awaitBlocked(t, 2)
	release()

	won, lost := await(t, first), await(t, second)
	if won.err != nil {
		won, lost = lost, won
	}
	if won.err != nil || won.request.State != StateAllowed || conflictReason(lost.err) != ConflictVersionChanged {
		t.Fatalf("decisions: %v, %v; want one refused", won.err, lost.err)
	}
	if f.count(t, `SELECT count(*) FROM action_decision`) != 1 || f.count(t, `SELECT count(*) FROM action_execution`) != 1 ||
		len(f.events(t, pending.ID)) != 2 {
		t.Error("the two decisions did not settle on one")
	}
}

func TestDifferentDecisionsOnOneVersionHaveOneWinner(t *testing.T) {
	for _, test := range []struct {
		name          string
		first, second Verdict
		state         ActionState
	}{
		{"allow queued first", VerdictAllow, VerdictDeny, StateAllowed},
		{"deny queued first", VerdictDeny, VerdictAllow, StateDenied},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "racing")
			release := f.holdRequest(t, pending.ID)

			decide := func(verdict Verdict) <-chan decided {
				return async(func() decided {
					request, err := f.Decide(t.Context(), pending.ID, decision(pending, verdict), operator)
					return decided{request, err}
				})
			}
			// Lock waiters are served in the order they queued.
			winner := decide(test.first)
			f.awaitBlocked(t, 1)
			loser := decide(test.second)
			f.awaitBlocked(t, 2)
			release()

			if won := await(t, winner); won.err != nil || won.request.State != test.state {
				t.Errorf("winner = %+v, %v", won.request.State, won.err)
			}
			if lost := await(t, loser); conflictReason(lost.err) != ConflictVersionChanged {
				t.Errorf("loser = %v", lost.err)
			}
			if f.get(t, pending.ID).State != test.state || f.count(t, `SELECT count(*) FROM action_decision`) != 1 {
				t.Error("the request holds more than the winner's decision")
			}
		})
	}
}

func TestAnApprovalCannotReviveACancelledRequest(t *testing.T) {
	for _, test := range []struct {
		name            string
		approvalFirst   bool
		approvalRefused bool
		states          []ActionState
		executions      int
	}{
		{"approval queued first", true, false, []ActionState{StateDecisionPending, StateAllowed, StateCancelled}, 1},
		{"cancellation queued first", false, true, []ActionState{StateDecisionPending, StateCancelled}, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "approved-or-cancelled")
			release := f.holdRequest(t, pending.ID)

			approve := func() <-chan decided {
				return async(func() decided {
					request, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow), operator)
					return decided{request, err}
				})
			}
			cancel := func() <-chan error {
				return async(func() error {
					result, err := f.Cancel(t.Context(), pending.ID, caller)
					if err == nil && result.Outcome != OutcomeCancelled {
						t.Errorf("cancellation outcome %s", result.Outcome)
					}
					return err
				})
			}
			var approval <-chan decided
			var cancellation <-chan error
			if test.approvalFirst {
				approval = approve()
				f.awaitBlocked(t, 1)
				cancellation = cancel()
			} else {
				cancellation = cancel()
				f.awaitBlocked(t, 1)
				approval = approve()
			}
			f.awaitBlocked(t, 2)
			release()

			if err := await(t, cancellation); err != nil {
				t.Errorf("cancellation: %v", err)
			}
			if approved := await(t, approval); test.approvalRefused != (conflictReason(approved.err) == ConflictVersionChanged) ||
				!test.approvalRefused && approved.err != nil {
				t.Errorf("approval: %v", approved.err)
			}
			final := f.get(t, pending.ID)
			if final.State != StateCancelled || !slices.Equal(eventStates(f.events(t, pending.ID)), test.states) {
				t.Errorf("state %s, events %v", final.State, eventStates(f.events(t, pending.ID)))
			}
			if f.count(t, `SELECT count(*) FROM action_execution`) != test.executions ||
				f.count(t, `SELECT count(*) FROM action_execution WHERE state <> 'cancelled'`) != 0 {
				t.Error("an execution survived the cancellation")
			}
		})
	}
}
