package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

const healthWindow = 30 * time.Second

func (f fixture) expire(t *testing.T) []uuid.UUID {
	t.Helper()
	expired, err := f.ExpireStaleLeases(t.Context(), healthWindow)
	if err != nil {
		t.Fatal(err)
	}
	return expired
}

func (f fixture) heartbeat(t *testing.T, id uuid.UUID, lease Lease) bool {
	t.Helper()
	renewed, err := f.HeartbeatExecution(t.Context(), id, lease, leaseWindow)
	if err != nil {
		t.Fatal(err)
	}
	return renewed
}

func TestMarkRunningCrossesTheNoReplayBoundary(t *testing.T) {
	f := newFixture(t)
	body := input("started")
	body.Arguments = json.RawMessage(`{"text": "hello", "api_key": "test-api-key"}`)
	pending := f.submitInput(t, caller, body, Admission{RequestID: uuid.New()})
	allowed := f.decide(t, pending, VerdictAllow)
	f.claim(t, allowed.ID)

	payload := f.markRunning(t, allowed.ID)
	if payload.RequestID != allowed.ID || payload.Action != body.Action || payload.Caller != caller.Account {
		t.Errorf("payload %+v", payload)
	}
	sameJSON(t, payload.Arguments, body.Arguments)
	got := f.get(t, allowed.ID)
	if got.State != action_servicepb.ActionState_ACTION_STATE_RUNNING || got.Version != allowed.Version+2 || got.Execution.State != ExecutionRunning ||
		got.Execution.StartedAt == nil || !got.Execution.StartedAt.Equal(got.UpdatedAt) {
		t.Errorf("request %+v, execution %+v", got, got.Execution)
	}
	if states := eventStates(f.events(t, allowed.ID)); !slices.Equal(states, []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_DISPATCHING, action_servicepb.ActionState_ACTION_STATE_RUNNING}) {
		t.Errorf("events %v", states)
	}

	unclaimed := f.allowed(t, caller, "unclaimed")
	for name, id := range map[string]uuid.UUID{"already started": allowed.ID, "not claimed": unclaimed.ID} {
		if _, err := f.MarkRunning(t.Context(), id); conflictReason(err) != ConflictNotStartable {
			t.Errorf("%s: %v", name, err)
		}
	}
	for name, id := range map[string]uuid.UUID{"unknown": uuid.New(), "with no execution": f.submit(t, caller, "undecided").ID} {
		if _, err := f.MarkRunning(t.Context(), id); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestHeartbeatRenewsOnlyTheOwnersLeaseOfAnExecutionInFlight(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "renewed")
	claim := f.claimFor(t, allowed.ID, -time.Second)
	f.markRunning(t, allowed.ID)

	if !f.heartbeat(t, allowed.ID, claim.Lease) {
		t.Fatal("the owner's heartbeat was refused")
	}
	if f.count(t, `SELECT count(*) FROM action_execution WHERE request_id = $1 AND lease_expires_at > now() AND heartbeat_at > $2`,
		allowed.ID, claim.ExpiresAt) != 1 {
		t.Error("the lease was not renewed")
	}
	if expired := f.expire(t); len(expired) != 0 || f.get(t, allowed.ID).State != action_servicepb.ActionState_ACTION_STATE_RUNNING {
		t.Errorf("a renewed lease expired: %v", expired)
	}

	for name, test := range map[string]struct {
		id    uuid.UUID
		lease Lease
	}{
		"another token":    {allowed.ID, Lease{ExecutorID: claim.ExecutorID, Token: uuid.New()}},
		"another executor": {allowed.ID, Lease{ExecutorID: "test-other-executor", Token: claim.Token}},
		"unknown request":  {uuid.New(), claim.Lease},
		"unclaimed":        {f.allowed(t, caller, "unclaimed").ID, claim.Lease},
	} {
		if f.heartbeat(t, test.id, test.lease) {
			t.Errorf("%s: the heartbeat was accepted", name)
		}
	}

	f.finish(t, allowed.ID, claim.Lease, ExecutionSucceeded)
	if f.heartbeat(t, allowed.ID, claim.Lease) {
		t.Error("a finished execution was renewed")
	}
}

func TestFinishRecordsTheTerminalStatePythonMapsItTo(t *testing.T) {
	result := json.RawMessage(`{"rows": [1, 2.50], "access_token": "test-access-token"}`)
	failure := json.RawMessage(`{"kind": "test_failure", "message": "test message"}`)
	for _, vector := range loadGolden(t).TerminalStates {
		t.Run(vector.ExecutionState, func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "finished")
			claim := f.claim(t, allowed.ID)
			f.markRunning(t, allowed.ID)

			outcome := ExecutionOutcome{State: ExecutionState(vector.ExecutionState), Result: result, Error: failure}
			if err := f.FinishExecution(t.Context(), allowed.ID, claim.Lease, outcome); err != nil {
				t.Fatal(err)
			}
			got := f.get(t, allowed.ID)
			execution := got.Execution
			if storedState(got.State) != vector.ActionState || got.Version != allowed.Version+3 || execution.State != outcome.State ||
				execution.CompletedAt == nil || !execution.CompletedAt.Equal(got.UpdatedAt) || execution.ReconciledAt != nil {
				t.Errorf("request %+v, execution %+v", got, execution)
			}
			sameJSON(t, execution.Result, result)
			sameJSON(t, execution.Error, failure)
			if states := eventStates(f.events(t, allowed.ID)); states[len(states)-1] != got.State {
				t.Errorf("events %v", states)
			}
		})
	}
}

func TestFinishWithoutAResultOrAnErrorStoresNull(t *testing.T) {
	f := newFixture(t)
	absent := f.allowed(t, caller, "absent")
	jsonNull := f.allowed(t, caller, "json-null")

	// An attempt may finish before MarkRunning recorded its start.
	f.finish(t, absent.ID, f.claim(t, absent.ID).Lease, ExecutionSucceeded)
	if err := f.FinishExecution(t.Context(), jsonNull.ID, f.claim(t, jsonNull.ID).Lease,
		ExecutionOutcome{State: ExecutionSucceeded, Result: json.RawMessage(`null`), Error: json.RawMessage(`null`)}); err != nil {
		t.Fatal(err)
	}
	if f.count(t, `SELECT count(*) FROM action_execution WHERE state = 'succeeded' AND result IS NULL AND error IS NULL`) != 2 {
		t.Error("an absent result or error is not NULL")
	}
}

func TestFinishRefusesWhatItCannotRecord(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "refused")
	claim := f.claim(t, allowed.ID)
	f.markRunning(t, allowed.ID)
	before := f.get(t, allowed.ID)

	for name, test := range map[string]struct {
		id      uuid.UUID
		lease   Lease
		outcome ExecutionOutcome
		want    func(error) bool
	}{
		"a state still in flight": {allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionRunning}, func(err error) bool { return errors.Is(err, ErrInvalid) }},
		"a result that is not JSON": {allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionSucceeded, Result: json.RawMessage(`{`)}, func(err error) bool {
			return errors.Is(err, ErrInvalid)
		}},
		"an error that is not an object": {allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionFailed, Error: json.RawMessage(`[1]`)}, func(err error) bool {
			return errors.Is(err, ErrInvalid)
		}},
		"another token": {allowed.ID, Lease{ExecutorID: claim.ExecutorID, Token: uuid.New()}, ExecutionOutcome{State: ExecutionSucceeded}, func(err error) bool {
			return conflictReason(err) == ConflictLeaseNotOwned
		}},
		"another executor": {allowed.ID, Lease{ExecutorID: "test-other-executor", Token: claim.Token}, ExecutionOutcome{State: ExecutionSucceeded}, func(err error) bool {
			return conflictReason(err) == ConflictLeaseNotOwned
		}},
		"an unclaimed execution": {f.allowed(t, caller, "unclaimed").ID, claim.Lease, ExecutionOutcome{State: ExecutionSucceeded}, func(err error) bool {
			return conflictReason(err) == ConflictLeaseNotOwned
		}},
		"an unknown request": {uuid.New(), claim.Lease, ExecutionOutcome{State: ExecutionSucceeded}, func(err error) bool { return errors.Is(err, ErrNotFound) }},
	} {
		if err := f.FinishExecution(t.Context(), test.id, test.lease, test.outcome); !test.want(err) {
			t.Errorf("%s: %v", name, err)
		}
	}
	after := f.get(t, allowed.ID)
	if after.State != before.State || after.Version != before.Version || after.Execution.State != ExecutionRunning {
		t.Error("a refused outcome changed the request")
	}

	f.finish(t, allowed.ID, claim.Lease, ExecutionSucceeded)
	if err := f.FinishExecution(t.Context(), allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionFailed}); conflictReason(err) != ConflictNotFinishable {
		t.Errorf("a second outcome: %v", err)
	}
	if got := f.get(t, allowed.ID); got.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
		t.Errorf("a second outcome changed the state to %s", got.State)
	}
}

func TestALateCompletionReconcilesTheUnknownExecution(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "late")
	claim := f.claimFor(t, allowed.ID, -time.Second)
	f.markRunning(t, allowed.ID)
	if expired := f.expire(t); !slices.Equal(expired, []uuid.UUID{allowed.ID}) {
		t.Fatalf("expired %v", expired)
	}
	unknown := f.get(t, allowed.ID)

	// The attempt was never abandoned: a different token does not reconcile it, the lease it was handed does.
	other := Lease{ExecutorID: claim.ExecutorID, Token: uuid.New()}
	if err := f.FinishExecution(t.Context(), allowed.ID, other, ExecutionOutcome{State: ExecutionSucceeded}); conflictReason(err) != ConflictLeaseNotOwned {
		t.Errorf("a late completion with another token: %v", err)
	}
	if f.get(t, allowed.ID).State != action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN {
		t.Fatal("a refused completion changed the state")
	}
	result := json.RawMessage(`{"echo": {}}`)
	if err := f.FinishExecution(t.Context(), allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionSucceeded, Result: result}); err != nil {
		t.Fatal(err)
	}

	got := f.get(t, allowed.ID)
	execution := got.Execution
	if got.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || execution.State != ExecutionSucceeded || execution.Error != nil ||
		execution.ReconciledAt == nil || !execution.ReconciledAt.Equal(got.UpdatedAt) ||
		!execution.CompletedAt.Equal(*unknown.Execution.CompletedAt) {
		t.Errorf("request %+v, execution %+v", got, execution)
	}
	sameJSON(t, execution.Result, result)
	want := []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED, action_servicepb.ActionState_ACTION_STATE_DISPATCHING, action_servicepb.ActionState_ACTION_STATE_RUNNING, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED}
	if states := eventStates(f.events(t, allowed.ID)); !slices.Equal(states, want) {
		t.Errorf("events %v, want %v", states, want)
	}
}

func TestTheSweepEndsALapsedLeaseAsUnknownAndNeverReplaysIt(t *testing.T) {
	for _, test := range []struct {
		name      string
		started   bool
		heartbeat time.Duration
		recorded  bool
		reason    UnknownOutcomeReason
	}{
		{"before the start, no executor heartbeat", false, 0, false, UnknownExecutorLost},
		{"after the start, a healthy executor", true, 0, true, UnknownLeaseExpired},
		{"after the start, a silent executor", true, time.Hour, true, UnknownExecutorLost},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "doomed")
			f.claimFor(t, allowed.ID, -time.Second)
			if test.started {
				f.markRunning(t, allowed.ID)
			}
			if test.recorded {
				if err := f.RecordExecutorHeartbeat(t.Context(), testExecutor); err != nil {
					t.Fatal(err)
				}
				f.exec(t, `UPDATE action_executor_heartbeat SET heartbeat_at = heartbeat_at - make_interval(secs => $1)`, test.heartbeat.Seconds())
			}

			if expired := f.expire(t); !slices.Equal(expired, []uuid.UUID{allowed.ID}) {
				t.Fatalf("expired %v", expired)
			}
			got := f.get(t, allowed.ID)
			execution := got.Execution
			if got.State != action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN || execution.State != ExecutionUnknown || execution.CompletedAt == nil ||
				!execution.CompletedAt.Equal(got.UpdatedAt) || execution.ReconciledAt != nil {
				t.Errorf("request %+v, execution %+v", got, execution)
			}
			for _, vector := range loadGolden(t).UnknownOutcomes {
				if vector.Reason == string(test.reason) {
					sameJSON(t, execution.Error, vector.Error)
				}
			}
			if len(f.pendingDispatches(t)) != 0 || len(f.expire(t)) != 0 {
				t.Error("the execution can be dispatched or expired again")
			}
		})
	}
}

func TestTheSweepLeavesLiveLeasesAndReturnsTheLapsedInRequestOrder(t *testing.T) {
	f := newFixture(t)
	live := f.allowed(t, caller, "live")
	f.claim(t, live.ID)
	var lapsed []uuid.UUID
	for _, key := range []string{"lapsed-a", "lapsed-b", "lapsed-c"} {
		allowed := f.allowed(t, caller, key)
		f.claimFor(t, allowed.ID, -time.Second)
		lapsed = append(lapsed, allowed.ID)
	}
	f.allowed(t, caller, "unclaimed")
	slices.SortFunc(lapsed, func(a, b uuid.UUID) int { return bytes.Compare(a[:], b[:]) })

	if expired := f.expire(t); !slices.Equal(expired, lapsed) {
		t.Errorf("expired %v, want %v", expired, lapsed)
	}
	if f.get(t, live.ID).State != action_servicepb.ActionState_ACTION_STATE_DISPATCHING {
		t.Error("a live lease was ended")
	}
}

func TestAnExecutorHeartbeatIsRecordedOnceAndRenewed(t *testing.T) {
	f := newFixture(t)
	if err := f.RecordExecutorHeartbeat(t.Context(), testExecutor); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE action_executor_heartbeat SET started_at = started_at - interval '1 hour', heartbeat_at = heartbeat_at - interval '1 hour'`)
	if err := f.RecordExecutorHeartbeat(t.Context(), testExecutor); err != nil {
		t.Fatal(err)
	}
	if f.count(t, `SELECT count(*) FROM action_executor_heartbeat WHERE started_at < now() - interval '59 minutes' AND heartbeat_at > now() - interval '1 minute'`) != 1 {
		t.Error("the second heartbeat did not renew the first row alone")
	}
	if err := f.RecordExecutorHeartbeat(t.Context(), ""); !errors.Is(err, ErrInvalid) {
		t.Errorf("a heartbeat without an executor: %v", err)
	}
}

type swept struct {
	ids []uuid.UUID
	err error
}

func TestASweepAndACompletionDoNotDeadlock(t *testing.T) {
	for _, finishFirst := range []bool{true, false} {
		name := "sweep queued first"
		if finishFirst {
			name = "completion queued first"
		}
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			allowed := f.allowed(t, caller, "crossed")
			claim := f.claimFor(t, allowed.ID, -time.Second)
			f.markRunning(t, allowed.ID)
			release := f.holdRequest(t, allowed.ID)

			finish := func() <-chan error {
				return async(func() error {
					return f.FinishExecution(t.Context(), allowed.ID, claim.Lease, ExecutionOutcome{State: ExecutionSucceeded})
				})
			}
			sweep := func() <-chan swept {
				return async(func() swept {
					ids, err := f.ExpireStaleLeases(t.Context(), healthWindow)
					return swept{ids, err}
				})
			}
			// Lock waiters are served in the order they queued.
			var finished <-chan error
			var sweeping <-chan swept
			if finishFirst {
				finished = finish()
				f.awaitBlocked(t, 1)
				sweeping = sweep()
			} else {
				sweeping = sweep()
				f.awaitBlocked(t, 1)
				finished = finish()
			}
			f.awaitBlocked(t, 2)
			release()

			if err := await(t, finished); err != nil {
				t.Errorf("completion: %v", err)
			}
			result := await(t, sweeping)
			if result.err != nil {
				t.Errorf("sweep: %v", result.err)
			}
			wantSwept := []uuid.UUID{allowed.ID}
			if finishFirst {
				wantSwept = nil
			}
			got := f.get(t, allowed.ID)
			if !slices.Equal(result.ids, wantSwept) || got.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || (got.Execution.ReconciledAt != nil) == finishFirst {
				t.Errorf("sweep expired %v, request %s, reconciled %v", result.ids, got.State, got.Execution.ReconciledAt)
			}
		})
	}
}
