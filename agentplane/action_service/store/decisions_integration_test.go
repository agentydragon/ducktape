package store

import (
	"errors"
	"reflect"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
)

func decision(request ActionRequest, verdict Verdict, key string) DecisionInput {
	return DecisionInput{Verdict: verdict, ExpectedVersion: request.Version, IdempotencyKey: key}
}

func TestDecideRecordsTheOperatorsVerdict(t *testing.T) {
	note := "test note"
	for _, test := range []struct {
		verdict Verdict
		state   action.State
	}{
		{VerdictAllow, action.StateAllowed},
		{VerdictDeny, action.StateDenied},
	} {
		t.Run(string(test.verdict), func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "decided")
			input := decision(pending, test.verdict, "test-decision")
			input.DecisionNote = &note

			got, dispatch, err := f.Decide(t.Context(), pending.ID, input, operator)
			if err != nil {
				t.Fatal(err)
			}
			awaitsDispatch := test.verdict == VerdictAllow
			if dispatch != awaitsDispatch || got.State != test.state || got.Version != 2 {
				t.Errorf("dispatch %v, state %s, version %d", dispatch, got.State, got.Version)
			}
			decided := got.Decision
			if decided == nil || decided.Verdict != test.verdict || decided.Provider != HumanProvider ||
				*decided.Operator != operator || *decided.DecisionNote != note || decided.ReasonCode != nil ||
				decided.ReasonDescription != nil || decided.PolicyEvidence != nil || decided.IdempotencyKey != "test-decision" ||
				!decided.DecidedAt.Equal(got.UpdatedAt) {
				t.Errorf("decision %+v", decided)
			}
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
			if !slices.Equal(eventStates(events), []action.State{action.StateDecisionPending, test.state}) || events[1].Actor != nil {
				t.Errorf("events %+v", events)
			}
		})
	}
}

func TestAReplayedDecisionChangesNothing(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "replayed")
	input := decision(pending, VerdictAllow, "test-decision")
	first, dispatch, err := f.Decide(t.Context(), pending.ID, input, operator)
	if err != nil || !dispatch {
		t.Fatalf("first decision: dispatch %v, %v", dispatch, err)
	}

	// The version in the replay is stale and the request may have moved on: the first Decision stands.
	replay, dispatch, err := f.Decide(t.Context(), pending.ID, input, operator)
	if err != nil || dispatch || !reflect.DeepEqual(replay, first) {
		t.Errorf("replay = %+v, dispatch %v, %v; want %+v", replay, dispatch, err, first)
	}
	if f.count(t, `SELECT count(*) FROM action_execution`) != 1 || len(f.events(t, pending.ID)) != 2 {
		t.Error("the replay wrote")
	}
	cancelled, err := f.Cancel(t.Context(), pending.ID, caller)
	if err != nil {
		t.Fatal(err)
	}
	replay, dispatch, err = f.Decide(t.Context(), pending.ID, input, operator)
	if err != nil || dispatch || !reflect.DeepEqual(replay, cancelled.Request) {
		t.Errorf("replay after the cancellation = %+v, dispatch %v, %v; want %+v", replay, dispatch, err, cancelled.Request)
	}
}

func TestADecisionKeyBelongsToItsOperatorAndItsFirstVerdict(t *testing.T) {
	f := newFixture(t)
	first := f.submit(t, caller, "first")
	second := f.submit(t, caller, "second")
	decided, _, err := f.Decide(t.Context(), first.ID, decision(first, VerdictAllow, "shared-key"), operator)
	if err != nil {
		t.Fatal(err)
	}

	for name, test := range map[string]struct {
		id    uuid.UUID
		input DecisionInput
	}{
		"the same operator's key for another verdict": {first.ID, decision(first, VerdictDeny, "shared-key")},
		"the same operator's key for another request": {second.ID, decision(second, VerdictAllow, "shared-key")},
	} {
		_, _, err := f.Decide(t.Context(), test.id, test.input, operator)
		if conflictReason(err) != ConflictDecisionKeyReused {
			t.Errorf("%s: %v", name, err)
		}
	}
	if !reflect.DeepEqual(f.get(t, first.ID), decided) || f.get(t, second.ID).State != action.StateDecisionPending {
		t.Error("a refused decision changed a request")
	}

	// Another operator's key is its own.
	other := OperatorPrincipal{Issuer: operator.Issuer, Subject: "test-other-operator"}
	if got, _, err := f.Decide(t.Context(), second.ID, decision(second, VerdictAllow, "shared-key"), other); err != nil || *got.Decision.Operator != other {
		t.Errorf("another operator's decision = %+v, %v", got.Decision, err)
	}
}

func TestADecisionNeedsTheCurrentVersionOfAPendingRequest(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "versioned")
	stale := decision(pending, VerdictAllow, "test-decision")
	stale.ExpectedVersion = pending.Version + 1
	if _, _, err := f.Decide(t.Context(), pending.ID, stale, operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("a version the request has not reached: %v", err)
	}
	if !reflect.DeepEqual(f.get(t, pending.ID), pending) || f.count(t, `SELECT count(*) FROM action_decision`) != 0 {
		t.Error("a refused decision wrote")
	}

	decided := f.decide(t, pending, VerdictAllow)
	// The version is checked first: an operator who saw an older request learns it changed.
	if _, _, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictDeny, "late-key"), operator); conflictReason(err) != ConflictVersionChanged {
		t.Errorf("an older version of a decided request: %v", err)
	}
	if _, _, err := f.Decide(t.Context(), pending.ID, decision(decided, VerdictDeny, "late-key"), operator); conflictReason(err) != ConflictAlreadyDecided {
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
		"provider allowed": f.submitInput(t, caller, input("provider-allowed"), Admission{RequestID: uuid.New(), Vote: vote(VerdictAllow)}),
		"cancelled":        f.get(t, cancelled.ID),
	} {
		t.Run(name, func(t *testing.T) {
			_, _, err := f.Decide(t.Context(), request.ID, decision(request, VerdictDeny, "another-key"), operator)
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
	valid := decision(pending, VerdictAllow, "test-decision")

	if _, _, err := f.Decide(t.Context(), uuid.New(), valid, operator); !errors.Is(err, ErrNotFound) {
		t.Errorf("an unknown request: %v", err)
	}
	for name, test := range map[string]struct {
		input    DecisionInput
		operator OperatorPrincipal
	}{
		"verdict":          {DecisionInput{Verdict: "maybe", ExpectedVersion: 1, IdempotencyKey: "test-decision"}, operator},
		"empty key":        {DecisionInput{Verdict: VerdictAllow, ExpectedVersion: 1}, operator},
		"version below 1":  {DecisionInput{Verdict: VerdictAllow, IdempotencyKey: "test-decision"}, operator},
		"anonymous issuer": {valid, OperatorPrincipal{Subject: "test-operator"}},
	} {
		if _, _, err := f.Decide(t.Context(), pending.ID, test.input, test.operator); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if f.get(t, pending.ID).State != action.StateDecisionPending {
		t.Error("a refused decision changed the request")
	}
}

func TestConcurrentIdenticalDecisionsDispatchOnce(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "twice")
	input := decision(pending, VerdictAllow, "test-decision")
	release := f.holdRequest(t, pending.ID)

	decide := func() <-chan decided {
		return async(func() decided {
			request, dispatch, err := f.Decide(t.Context(), pending.ID, input, operator)
			return decided{request, dispatch, err}
		})
	}
	first, second := decide(), decide()
	f.awaitBlocked(t, 2)
	release()

	a, b := await(t, first), await(t, second)
	if a.err != nil || b.err != nil {
		t.Fatalf("decisions: %v, %v", a.err, b.err)
	}
	if a.dispatch == b.dispatch {
		t.Errorf("dispatch = %v and %v, want exactly one", a.dispatch, b.dispatch)
	}
	if !reflect.DeepEqual(a.request, b.request) || f.count(t, `SELECT count(*) FROM action_execution`) != 1 ||
		len(f.events(t, pending.ID)) != 2 {
		t.Error("the two decisions did not settle on one")
	}
}

func TestDifferentDecisionsOnOneVersionHaveOneWinner(t *testing.T) {
	for _, test := range []struct {
		name          string
		first, second Verdict
		state         action.State
	}{
		{"allow queued first", VerdictAllow, VerdictDeny, action.StateAllowed},
		{"deny queued first", VerdictDeny, VerdictAllow, action.StateDenied},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "racing")
			release := f.holdRequest(t, pending.ID)

			decide := func(verdict Verdict) <-chan decided {
				return async(func() decided {
					request, dispatch, err := f.Decide(t.Context(), pending.ID, decision(pending, verdict, "key-"+string(verdict)), operator)
					return decided{request, dispatch, err}
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
		states          []action.State
		executions      int
	}{
		{"approval queued first", true, false, []action.State{action.StateDecisionPending, action.StateAllowed, action.StateCancelled}, 1},
		{"cancellation queued first", false, true, []action.State{action.StateDecisionPending, action.StateCancelled}, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			pending := f.submit(t, caller, "approved-or-cancelled")
			release := f.holdRequest(t, pending.ID)

			approve := func() <-chan decided {
				return async(func() decided {
					request, dispatch, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow, "test-decision"), operator)
					return decided{request, dispatch, err}
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
			if final.State != action.StateCancelled || !slices.Equal(eventStates(f.events(t, pending.ID)), test.states) {
				t.Errorf("state %s, events %v", final.State, eventStates(f.events(t, pending.ID)))
			}
			if f.count(t, `SELECT count(*) FROM action_execution`) != test.executions ||
				f.count(t, `SELECT count(*) FROM action_execution WHERE state <> 'cancelled'`) != 0 {
				t.Error("an execution survived the cancellation")
			}
		})
	}
}
