package store

import (
	"encoding/json"
	"reflect"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
)

// These tests write rows the way the Python service does, from the golden vectors it produced, and hold the
// store to reading them, and to writing the same documents back: the store takes over a database that Python
// filled.

// insertRequest writes a pending request and the Event Python's insert listener records for it.
func (f fixture) insertRequest(t *testing.T, id uuid.UUID, owner action.ServiceAccount, key string, stored storedRequest) {
	t.Helper()
	f.exec(t, `
		INSERT INTO action_request (id, idempotency_key, action, arguments, title, description, caller_namespace, caller_name,
			state, version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, 'decision_pending', 1, now(), now())`,
		id, key, string(stored.Action), string(stored.Arguments), stored.Title, stored.Description, owner.Namespace,
		owner.Name)
	f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at) VALUES ($1, 1, 'decision_pending', now())`, id)
}

var minimalRequest = storedRequest{
	Action:    json.RawMessage(`{"group": "test-group", "name": "test-action"}`),
	Arguments: json.RawMessage(`{}`),
	Title:     "test title",
}

func (f fixture) insertMinimalRequest(t *testing.T, owner action.ServiceAccount, key string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	f.insertRequest(t, id, owner, key, minimalRequest)
	return id
}

// document is the JSON a query selects for the request, as PostgreSQL holds it.
func (f fixture) document(t *testing.T, query string, id uuid.UUID) []byte {
	t.Helper()
	var document []byte
	if err := f.db.QueryRowContext(t.Context(), query, id).Scan(&document); err != nil {
		t.Fatal(err)
	}
	return document
}

func TestRequestsPythonStoredAreReadAndWrittenBack(t *testing.T) {
	for _, vector := range loadGolden(t).Requests {
		if !vector.Valid {
			continue
		}
		t.Run(vector.Name, func(t *testing.T) {
			f := newFixture(t)
			sent := requestInput(t, vector.Input)
			id := uuid.New()
			f.insertRequest(t, id, otherCaller.Account, sent.IdempotencyKey, vector.Stored)

			read := f.get(t, id)
			stored, err := json.Marshal(read.Action)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, stored, vector.Stored.Action)
			sameJSON(t, read.Arguments, vector.Stored.Arguments)
			if read.Title != vector.Stored.Title || !reflect.DeepEqual(read.Description, vector.Stored.Description) {
				t.Errorf("read title and description %q, %v; Python stored %q, %v", read.Title, read.Description,
					vector.Stored.Title, vector.Stored.Description)
			}

			written := f.submitInput(t, caller, sent, Admission{RequestID: uuid.New()})
			sameJSON(t, f.document(t, `SELECT action FROM action_request WHERE id = $1`, written.ID), vector.Stored.Action)
			sameJSON(t, f.document(t, `SELECT arguments FROM action_request WHERE id = $1`, written.ID), vector.Stored.Arguments)
			if f.count(t, `SELECT count(*) FROM action_request WHERE id = $1 AND title = $2 AND description IS NOT DISTINCT FROM $3`,
				written.ID, vector.Stored.Title, vector.Stored.Description) != 1 {
				t.Errorf("the title and description were not stored as Python stored them")
			}
		})
	}
}

// decideAsPython records a Decision on the pending request the way Python does, with the document as its policy
// evidence.
func (f fixture) decideAsPython(t *testing.T, id uuid.UUID, evidence json.RawMessage) {
	t.Helper()
	f.exec(t, `
		INSERT INTO action_decision (id, request_id, verdict, provider, idempotency_key, decided_at, reason_code, policy_evidence)
		VALUES ($1, $2, 'deny', 'test-provider', $3, now(), 'test_reason', $4::jsonb)`,
		uuid.New(), id, "auto:"+id.String(), jsonParam(evidence))
	f.exec(t, `UPDATE action_request SET state = 'denied', version = 2, updated_at = now(), history_at = now() WHERE id = $1`, id)
	f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at) VALUES ($1, 2, 'denied', now())`, id)
}

func TestPolicyEvidencePythonStoredIsReadAndWrittenBack(t *testing.T) {
	for _, vector := range loadGolden(t).PolicyEvidence {
		if !vector.Valid {
			continue
		}
		t.Run(vector.Name, func(t *testing.T) {
			f := newFixture(t)
			var evidence PolicyEvidence
			if err := json.Unmarshal(vector.Input, &evidence); err != nil {
				t.Fatal(err)
			}
			id := f.insertMinimalRequest(t, otherCaller.Account, "python-decided")
			f.decideAsPython(t, id, vector.Stored)
			if read := f.get(t, id).Decision; read == nil || !reflect.DeepEqual(read.PolicyEvidence, &evidence) {
				t.Errorf("read evidence %+v, want %+v", read, evidence)
			}

			written := f.submitInput(t, caller, input("go-decided"), Admission{RequestID: uuid.New(), Vote: &ProviderVote{
				Provider: "test-provider", Verdict: VerdictDeny, ReasonCode: "test_reason", Evidence: &evidence,
			}})
			sameJSON(t, f.document(t, `SELECT policy_evidence FROM action_decision WHERE request_id = $1`, written.ID), vector.Stored)
		})
	}
}

func TestActorsPythonStoredAreReadAndWrittenBack(t *testing.T) {
	f := newFixture(t)
	for _, vector := range loadGolden(t).Actors {
		if !vector.Valid {
			continue
		}
		t.Run(vector.Name, func(t *testing.T) {
			id := f.insertMinimalRequest(t, otherCaller.Account, "python-"+vector.Name)
			f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at, actor) VALUES ($1, 2, 'cancelled', now(), $2::jsonb)`,
				id, string(vector.Stored))
			events := f.events(t, id)
			if len(events) != 2 || events[1].Actor == nil {
				t.Fatalf("events %+v", events)
			}
			if _, isCaller := events[1].Actor.(action.CallerActor); isCaller != (vector.Kind == "caller") {
				t.Errorf("read a %T, want a %s", events[1].Actor, vector.Kind)
			}

			if vector.Kind != "caller" {
				return
			}
			actor, err := action.DecodeActor(vector.Input)
			if err != nil {
				t.Fatal(err)
			}
			owner := CallerPrincipal{Account: actor.(action.CallerActor).Account}
			written := f.submitInput(t, owner, input("go-"+vector.Name), Admission{RequestID: uuid.New()})
			if _, err := f.Cancel(t.Context(), written.ID, owner); err != nil {
				t.Fatal(err)
			}
			sameJSON(t, f.document(t, `SELECT actor FROM action_event WHERE request_id = $1 AND sequence = 2`, written.ID), vector.Stored)
		})
	}
}

func TestAbsentValuesAreWrittenAsNull(t *testing.T) {
	f := newFixture(t)
	f.allowed(t, caller, "absent")

	for _, test := range []struct {
		name  string
		query string
		rows  int
	}{
		{"request", `SELECT count(*) FROM action_request WHERE description IS NULL`, 1},
		{"events", `SELECT count(*) FROM action_event WHERE actor IS NULL`, 2},
		{"decision", `SELECT count(*) FROM action_decision
			WHERE decision_note IS NULL AND reason_code IS NULL AND reason_description IS NULL AND policy_evidence IS NULL`, 1},
		{"execution", `SELECT count(*) FROM action_execution
			WHERE result IS NULL AND error IS NULL AND started_at IS NULL AND completed_at IS NULL AND executor_id IS NULL
				AND lease_token IS NULL AND lease_expires_at IS NULL AND heartbeat_at IS NULL AND reconciled_at IS NULL`, 1},
	} {
		if got := f.count(t, test.query); got != test.rows {
			t.Errorf("%s: %d rows hold only NULLs for what is absent, want %d", test.name, got, test.rows)
		}
	}
}

func TestAJSONNullIsAnAbsentValue(t *testing.T) {
	f := newFixture(t)
	allowed := f.allowed(t, caller, "json-null")
	f.exec(t, `UPDATE action_event SET actor = 'null'::jsonb WHERE request_id = $1`, allowed.ID)
	f.exec(t, `UPDATE action_decision SET policy_evidence = 'null'::jsonb WHERE request_id = $1`, allowed.ID)
	f.exec(t, `UPDATE action_execution SET state = 'succeeded', result = 'null'::jsonb, error = 'null'::jsonb WHERE request_id = $1`, allowed.ID)

	read := f.get(t, allowed.ID)
	if read.Decision.PolicyEvidence != nil || read.Execution.Result != nil || read.Execution.Error != nil {
		t.Errorf("read %+v, %+v, %+v", read, read.Decision, read.Execution)
	}
	if actors := f.events(t, allowed.ID); actors[0].Actor != nil || actors[1].Actor != nil {
		t.Errorf("events %+v", actors)
	}
}

// insertAllowedAsPython writes a request an operator allowed, the way Python writes it: its Decision, its Events and
// the Execution that awaits dispatch.
func (f fixture) insertAllowedAsPython(t *testing.T, key string) uuid.UUID {
	t.Helper()
	id := f.insertMinimalRequest(t, caller.Account, key)
	f.exec(t, `
		INSERT INTO action_decision (id, request_id, verdict, provider, operator_issuer, operator_subject, idempotency_key, decided_at)
		VALUES ($1, $2, 'allow', 'human_operator', $3, $4, $5, now())`,
		uuid.New(), id, operator.Issuer, operator.Subject, "python-decision-"+key)
	f.exec(t, `UPDATE action_request SET state = 'allowed', version = 2, updated_at = now(), history_at = now() WHERE id = $1`, id)
	f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at) VALUES ($1, 2, 'allowed', now())`, id)
	f.exec(t, `INSERT INTO action_execution (id, request_id, state, created_at) VALUES ($1, $2, 'pending_dispatch', now())`, uuid.New(), id)
	return id
}

// insertInFlightAsPython writes an allowed request Python then took through the states, with a lapsed lease.
func (f fixture) insertInFlightAsPython(t *testing.T, key string, states []action.State, lease Lease, failure json.RawMessage) uuid.UUID {
	t.Helper()
	id := f.insertAllowedAsPython(t, key)
	for i, state := range states {
		f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at) VALUES ($1, $2, $3, now())`, id, 3+i, state)
	}
	last := states[len(states)-1]
	f.exec(t, `UPDATE action_request SET state = $2, version = $3, updated_at = now() WHERE id = $1`, id, last, 2+len(states))
	f.exec(t, `
		UPDATE action_execution
		SET state = $2, executor_id = $3, lease_token = $4, lease_expires_at = now() - interval '1 minute',
			heartbeat_at = now() - interval '2 minutes', error = $5::jsonb
		WHERE request_id = $1`,
		id, ExecutionState(last), lease.ExecutorID, lease.Token, jsonParam(failure))
	return id
}

func TestExecutionsPythonLeftAreDispatchedSweptAndReconciled(t *testing.T) {
	f := newFixture(t)
	queued := f.insertAllowedAsPython(t, "python-queued")
	lostLease := Lease{ExecutorID: "test-python-executor", Token: uuid.New()}
	lost := f.insertInFlightAsPython(t, "python-lost", []action.State{action.StateDispatching, action.StateRunning}, lostLease, nil)
	sweptLease := Lease{ExecutorID: "test-python-executor", Token: uuid.New()}
	pythonError := loadGolden(t).UnknownOutcomes[0].Error
	swept := f.insertInFlightAsPython(t, "python-swept", []action.State{action.StateDispatching, action.StateRunning, action.StateExecutionUnknown}, sweptLease, pythonError)

	if got := f.pendingDispatches(t); !slices.Equal(got, []uuid.UUID{queued}) {
		t.Fatalf("pending dispatches %v, want only %v", got, queued)
	}
	f.claim(t, queued)
	f.markRunning(t, queued)
	if expired := f.expire(t); !slices.Equal(expired, []uuid.UUID{lost}) {
		t.Errorf("the sweep ended %v, want only the attempt Python left in flight, %v", expired, lost)
	}
	sameJSON(t, f.get(t, swept).Execution.Error, pythonError)

	for name, finished := range map[string]struct {
		id    uuid.UUID
		lease Lease
	}{"swept by Python": {swept, sweptLease}, "swept by Go": {lost, lostLease}} {
		f.finish(t, finished.id, finished.lease, ExecutionSucceeded)
		if got := f.get(t, finished.id); got.State != action.StateSucceeded || got.Execution.ReconciledAt == nil {
			t.Errorf("%s: request %s, execution %+v", name, got.State, got.Execution)
		}
	}
}

func TestRequestsPythonWroteCanBeDecidedAndCancelled(t *testing.T) {
	f := newFixture(t)
	toDecide := f.insertMinimalRequest(t, caller.Account, "python-to-decide")
	toCancel := f.insertMinimalRequest(t, caller.Account, "python-to-cancel")

	pending := f.get(t, toDecide)
	decided, dispatch, err := f.Decide(t.Context(), toDecide, decision(pending, VerdictAllow, "test-decision"), operator)
	if err != nil || !dispatch || decided.State != action.StateAllowed || decided.Version != 2 {
		t.Fatalf("decision = %+v, dispatch %v, %v", decided, dispatch, err)
	}
	cancelled := f.cancel(t, toCancel)
	if cancelled.Outcome != OutcomeCancelled || cancelled.Request.Version != 2 {
		t.Errorf("cancellation = %+v", cancelled)
	}
	for id, states := range map[uuid.UUID][]action.State{
		toDecide: {action.StateDecisionPending, action.StateAllowed},
		toCancel: {action.StateDecisionPending, action.StateCancelled},
	} {
		if got := eventStates(f.events(t, id)); !slices.Equal(got, states) {
			t.Errorf("events of %s = %v, want %v", id, got, states)
		}
	}
}
