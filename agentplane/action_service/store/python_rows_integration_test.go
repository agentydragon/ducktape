package store

import (
	"encoding/json"
	"reflect"
	"slices"
	"testing"

	"github.com/google/uuid"
)

// These tests write rows the way the Python service does, from the golden vectors it produced, and hold the
// store to reading them, and to writing the same documents back: the store takes over a database that Python
// filled.

// jsonParam is a JSONB column's parameter: NULL when there is no document.
func jsonParam(document json.RawMessage) any {
	if document == nil {
		return nil
	}
	return string(document)
}

// insertRequest writes a pending request and the Event Python's insert listener records for it.
func (f fixture) insertRequest(t *testing.T, id uuid.UUID, owner ServiceAccount, key string, stored storedRequest, grant json.RawMessage) {
	t.Helper()
	f.exec(t, `
		INSERT INTO action_request (id, idempotency_key, action, arguments, title, description, caller_namespace, caller_name,
			external_grant, state, version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9::jsonb, 'decision_pending', 1, now(), now())`,
		id, key, string(stored.Action), string(stored.Arguments), stored.Title, stored.Description, owner.Namespace,
		owner.Name, jsonParam(grant))
	f.exec(t, `INSERT INTO action_event (request_id, sequence, state, at) VALUES ($1, 1, 'decision_pending', now())`, id)
}

var minimalRequest = storedRequest{
	Action:    json.RawMessage(`{"group": "test-group", "name": "test-action"}`),
	Arguments: json.RawMessage(`{}`),
	Title:     "test title",
}

func (f fixture) insertMinimalRequest(t *testing.T, owner ServiceAccount, key string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	f.insertRequest(t, id, owner, key, minimalRequest, nil)
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
			f.insertRequest(t, id, otherCaller.Account, sent.IdempotencyKey, vector.Stored, nil)

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

func TestExternalGrantsPythonStoredAreReadAndWrittenBack(t *testing.T) {
	for _, vector := range loadGolden(t).ExternalGrants {
		if !vector.Valid {
			continue
		}
		t.Run(vector.Name, func(t *testing.T) {
			f := newFixture(t)
			var grant ExternalGrant
			if err := json.Unmarshal(vector.Input, &grant); err != nil {
				t.Fatal(err)
			}
			id := uuid.New()
			f.insertRequest(t, id, grant.Caller, "python-granted", minimalRequest, vector.Stored)
			if read := f.get(t, id); read.ExternalGrant == nil || *read.ExternalGrant != grant {
				t.Errorf("read grant %+v, want %+v", read.ExternalGrant, grant)
			}

			f.grants.set(grant, true)
			written := f.submitInput(t, grant.Principal(), input("go-granted"), Admission{RequestID: uuid.New(), ExternalGrant: &grant})
			sameJSON(t, f.document(t, `SELECT external_grant FROM action_request WHERE id = $1`, written.ID), vector.Stored)
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
			if _, isCaller := events[1].Actor.(CallerPrincipal); isCaller != (vector.Kind == "caller") {
				t.Errorf("read a %T, want a %s", events[1].Actor, vector.Kind)
			}

			if vector.Kind != "caller" {
				return
			}
			actor, err := decodeActor(vector.Input)
			if err != nil {
				t.Fatal(err)
			}
			owner := actor.(CallerPrincipal)
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
		{"request", `SELECT count(*) FROM action_request WHERE description IS NULL AND external_grant IS NULL`, 1},
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
	f.exec(t, `UPDATE action_request SET external_grant = 'null'::jsonb WHERE id = $1`, allowed.ID)
	f.exec(t, `UPDATE action_event SET actor = 'null'::jsonb WHERE request_id = $1`, allowed.ID)
	f.exec(t, `UPDATE action_decision SET policy_evidence = 'null'::jsonb WHERE request_id = $1`, allowed.ID)
	f.exec(t, `UPDATE action_execution SET state = 'succeeded', result = 'null'::jsonb, error = 'null'::jsonb WHERE request_id = $1`, allowed.ID)

	read := f.get(t, allowed.ID)
	if read.ExternalGrant != nil || read.Decision.PolicyEvidence != nil || read.Execution.Result != nil || read.Execution.Error != nil {
		t.Errorf("read %+v, %+v, %+v", read, read.Decision, read.Execution)
	}
	if actors := f.events(t, allowed.ID); actors[0].Actor != nil || actors[1].Actor != nil {
		t.Errorf("events %+v", actors)
	}
}

func TestRequestsPythonWroteCanBeDecidedAndCancelled(t *testing.T) {
	f := newFixture(t)
	toDecide := f.insertMinimalRequest(t, caller.Account, "python-to-decide")
	toCancel := f.insertMinimalRequest(t, caller.Account, "python-to-cancel")

	pending := f.get(t, toDecide)
	decided, dispatch, err := f.Decide(t.Context(), toDecide, decision(pending, VerdictAllow, "test-decision"), operator)
	if err != nil || !dispatch || decided.State != StateAllowed || decided.Version != 2 {
		t.Fatalf("decision = %+v, dispatch %v, %v", decided, dispatch, err)
	}
	cancelled := f.cancel(t, toCancel)
	if cancelled.Outcome != OutcomeCancelled || cancelled.Request.Version != 2 {
		t.Errorf("cancellation = %+v", cancelled)
	}
	for id, states := range map[uuid.UUID][]ActionState{
		toDecide: {StateDecisionPending, StateAllowed},
		toCancel: {StateDecisionPending, StateCancelled},
	} {
		if got := eventStates(f.events(t, id)); !slices.Equal(got, states) {
			t.Errorf("events of %s = %v, want %v", id, got, states)
		}
	}
}
