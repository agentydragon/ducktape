package store

import (
	"errors"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

const (
	testExecutor = "test-executor"
	leaseWindow  = 30 * time.Second
)

func anywhere(ActionIdentity) bool { return true }

func (f fixture) claim(t *testing.T, id uuid.UUID) ExecutionClaim {
	t.Helper()
	return f.claimFor(t, id, leaseWindow)
}

// claimFor claims the Execution for a lease window of the given length, which may already have lapsed.
func (f fixture) claimFor(t *testing.T, id uuid.UUID, window time.Duration) ExecutionClaim {
	t.Helper()
	claim, err := f.ClaimExecution(t.Context(), id, testExecutor, window, anywhere)
	if err != nil {
		t.Fatal(err)
	}
	if claim == nil {
		t.Fatal("the execution was not claimed")
	}
	return *claim
}

func (f fixture) markRunning(t *testing.T, id uuid.UUID) ExecutionRequest {
	t.Helper()
	payload, err := f.MarkRunning(t.Context(), id)
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func (f fixture) finish(t *testing.T, id uuid.UUID, lease Lease, state ExecutionState) {
	t.Helper()
	if err := f.FinishExecution(t.Context(), id, lease, ExecutionOutcome{State: state}); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) pendingDispatches(t *testing.T) []uuid.UUID {
	t.Helper()
	ids, err := f.PendingDispatches(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	return ids
}

func TestClaimReservesTheOnlyExecutionAndGrantsItsLease(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "claimed")

	before := time.Now()
	claim := f.claim(t, allowed.ID)
	after := time.Now()
	if claim.RequestID != allowed.ID || claim.ExecutorID != testExecutor || claim.Token == uuid.Nil ||
		claim.ExpiresAt.Before(before.Add(leaseWindow).Add(-time.Millisecond)) || claim.ExpiresAt.After(after.Add(leaseWindow)) {
		t.Errorf("claim %+v", claim)
	}
	got := f.get(t, allowed.ID)
	if got.State != action_servicepb.ActionState_ACTION_STATE_DISPATCHING || got.Version != allowed.Version+1 || got.Execution.State != ExecutionDispatching ||
		got.Execution.StartedAt != nil || got.Execution.CompletedAt != nil {
		t.Errorf("request %+v, execution %+v", got, got.Execution)
	}
	if f.count(t, `SELECT count(*) FROM action_execution
		WHERE request_id = $1 AND executor_id = $2 AND lease_token = $3 AND lease_expires_at = $4 AND heartbeat_at IS NOT NULL`,
		allowed.ID, claim.ExecutorID, claim.Token, claim.ExpiresAt) != 1 {
		t.Error("the lease was not recorded as the claim reports it")
	}
	events := f.events(t, allowed.ID)
	if !slices.Equal(eventStates(events), []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_DISPATCHING}) ||
		events[2].Actor != nil || !events[2].At.Equal(got.UpdatedAt) {
		t.Errorf("events %+v", events)
	}
	if len(f.pendingDispatches(t)) != 0 {
		t.Error("a claimed execution still awaits dispatch")
	}

	// The Execution has one claim: another executor finds nothing to dispatch and the lease stays the first's.
	second, err := f.ClaimExecution(t.Context(), allowed.ID, "test-other-executor", leaseWindow, anywhere)
	if err != nil || second != nil {
		t.Errorf("second claim = %+v, %v", second, err)
	}
	if len(f.events(t, allowed.ID)) != 3 || f.count(t, `SELECT count(*) FROM action_execution WHERE lease_token = $1`, claim.Token) != 1 {
		t.Error("the second claim wrote")
	}
}

func TestClaimRefusesWhatItCannotReserve(t *testing.T) {
	f := newFixture(t)
	pending := f.submit(t, caller, "pending")
	stranded := f.allowed(t, caller, "stranded")
	f.exec(t, `UPDATE action_request SET state = 'cancelled' WHERE id = $1`, stranded.ID)

	for name, test := range map[string]struct {
		id       uuid.UUID
		executor string
		filter   func(ActionIdentity) bool
		want     func(error) bool
	}{
		"an unknown request":             {uuid.New(), testExecutor, anywhere, func(err error) bool { return errors.Is(err, ErrNotFound) }},
		"a request that was not decided": {pending.ID, testExecutor, anywhere, func(err error) bool { return errors.Is(err, ErrNotFound) }},
		"an execution of a request that is not allowed": {stranded.ID, testExecutor, anywhere, func(err error) bool {
			return conflictReason(err) == ConflictNotDispatchable
		}},
		"no executor": {stranded.ID, "", anywhere, func(err error) bool { return errors.Is(err, ErrInvalid) }},
		"no filter":   {stranded.ID, testExecutor, nil, func(err error) bool { return errors.Is(err, ErrInvalid) }},
	} {
		claim, err := f.ClaimExecution(t.Context(), test.id, test.executor, leaseWindow, test.filter)
		if claim != nil || !test.want(err) {
			t.Errorf("%s: claim %+v, %v", name, claim, err)
		}
	}
	if f.count(t, `SELECT count(*) FROM action_execution WHERE state = 'pending_dispatch'`) != 1 {
		t.Error("a refused claim changed an execution")
	}
}

func TestClaimLeavesWhatTheDispatchFilterRefusesToBeClaimedElsewhere(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "filtered")
	var asked []ActionIdentity

	claim, err := f.ClaimExecution(t.Context(), allowed.ID, testExecutor, leaseWindow, func(action ActionIdentity) bool {
		asked = append(asked, action)
		return false
	})
	if err != nil || claim != nil || !reflect.DeepEqual(asked, []ActionIdentity{allowed.Action}) {
		t.Errorf("claim %+v, %v after the filter was asked about %v", claim, err, asked)
	}
	if !reflect.DeepEqual(f.get(t, allowed.ID), allowed) || !slices.Equal(f.pendingDispatches(t), []uuid.UUID{allowed.ID}) {
		t.Error("a refused claim changed the request")
	}
	f.claim(t, allowed.ID)
}

func TestTwoClaimersHaveOneWinner(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "contended")
	release := f.holdRequest(t, allowed.ID)

	claimant := func(executor string) <-chan *ExecutionClaim {
		return async(func() *ExecutionClaim {
			claim, err := f.ClaimExecution(t.Context(), allowed.ID, executor, leaseWindow, anywhere)
			if err != nil {
				t.Errorf("%s: %v", executor, err)
			}
			return claim
		})
	}
	first, second := claimant("test-executor-a"), claimant("test-executor-b")
	f.awaitBlocked(t, 2)
	release()

	a, b := await(t, first), await(t, second)
	if (a == nil) == (b == nil) {
		t.Fatalf("claims %+v and %+v, want exactly one", a, b)
	}
	winner := a
	if winner == nil {
		winner = b
	}
	if f.count(t, `SELECT count(*) FROM action_execution WHERE executor_id = $1 AND lease_token = $2`, winner.ExecutorID, winner.Token) != 1 ||
		len(f.events(t, allowed.ID)) != 3 {
		t.Error("the execution does not hold exactly the winner's claim")
	}
}

func TestAClaimAndACancellationHaveOneWinner(t *testing.T) {
	for _, claimFirst := range []bool{true, false} {
		name := "cancellation queued first"
		if claimFirst {
			name = "claim queued first"
		}
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "raced")
			release := f.holdRequest(t, allowed.ID)

			claim := func() <-chan *ExecutionClaim {
				return async(func() *ExecutionClaim {
					claim, err := f.ClaimExecution(t.Context(), allowed.ID, testExecutor, leaseWindow, anywhere)
					if err != nil {
						t.Error(err)
					}
					return claim
				})
			}
			cancel := func() <-chan CancellationResult {
				return async(func() CancellationResult {
					result, err := f.Cancel(t.Context(), allowed.ID, caller)
					if err != nil {
						t.Error(err)
					}
					return result
				})
			}
			var claimed <-chan *ExecutionClaim
			var cancelled <-chan CancellationResult
			// Lock waiters are served in the order they queued.
			if claimFirst {
				claimed = claim()
				f.awaitBlocked(t, 1)
				cancelled = cancel()
			} else {
				cancelled = cancel()
				f.awaitBlocked(t, 1)
				claimed = claim()
			}
			f.awaitBlocked(t, 2)
			release()

			got, cancellation := await(t, claimed), await(t, cancelled)
			wantOutcome, wantState := OutcomeTooLate, action_servicepb.ActionState_ACTION_STATE_DISPATCHING
			if !claimFirst {
				wantOutcome, wantState = OutcomeCancelled, action_servicepb.ActionState_ACTION_STATE_CANCELLED
			}
			if (got != nil) != claimFirst || cancellation.Outcome != wantOutcome || f.get(t, allowed.ID).State != wantState {
				t.Errorf("claim %+v, cancellation %s, state %s", got, cancellation.Outcome, f.get(t, allowed.ID).State)
			}
			if !claimFirst && len(f.pendingDispatches(t)) != 0 {
				t.Error("a cancelled request still awaits dispatch")
			}
		})
	}
}

func TestPendingDispatchesAreTheAllowedRequestsNobodyClaimedOldestFirst(t *testing.T) {
	f := newFixture(t)
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	var waiting []uuid.UUID
	for i, key := range []string{"newest", "oldest", "middle"} {
		allowed := f.allowed(t, caller, key)
		f.exec(t, `UPDATE action_execution SET created_at = $2 WHERE request_id = $1`, allowed.ID, base.Add([]time.Duration{2, 0, 1}[i]*time.Minute))
		waiting = append(waiting, allowed.ID)
	}
	claimed, cancelled := f.allowed(t, caller, "claimed"), f.allowed(t, caller, "cancelled")
	f.claim(t, claimed.ID)
	f.cancel(t, cancelled.ID)
	f.decide(t, f.submit(t, caller, "denied"), VerdictDeny)
	f.submit(t, caller, "undecided")

	want := []uuid.UUID{waiting[1], waiting[2], waiting[0]}
	if got := f.pendingDispatches(t); !slices.Equal(got, want) {
		t.Errorf("pending dispatches = %v, want %v", got, want)
	}
}
