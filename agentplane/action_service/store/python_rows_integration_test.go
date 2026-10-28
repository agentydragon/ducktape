package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

// These tests use the Python-produced golden vectors to hold the typed store to the same request, decision and
// native JSON behavior as the former service.

// insertRequest writes a pending request and the Event Python's insert listener records for it.
func (f fixture) insertRequest(t *testing.T, id uuid.UUID, owner ServiceAccount, key string, stored storedRequest) {
	t.Helper()
	var action ActionIdentity
	if err := json.Unmarshal(stored.Action, &action); err != nil {
		t.Fatal(err)
	}
	f.exec(t, `
		INSERT INTO action_request (id, idempotency_key, action_group, action_name, arguments, title, description,
			caller_namespace, caller_name, state, version, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, 'decision_pending', 1, now(), now())`,
		id, key, action.Group, action.Name, string(stored.Arguments), stored.Title, stored.Description, owner.Namespace,
		owner.Name)
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
			var group, name string
			if err := f.db.QueryRowContext(t.Context(), `SELECT action_group, action_name FROM action_request WHERE id = $1`,
				written.ID).Scan(&group, &name); err != nil {
				t.Fatal(err)
			}
			if group != sent.Action.Group || name != sent.Action.Name {
				t.Errorf("typed action identity = %q/%q, want %q/%q", group, name, sent.Action.Group, sent.Action.Name)
			}
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
	var decoded *PolicyEvidence
	if err := json.Unmarshal(evidence, &decoded); err != nil {
		t.Fatal(err)
	}
	encoded, err := encodePolicyEvidence(decoded)
	if err != nil {
		t.Fatal(err)
	}
	f.exec(t, `
		INSERT INTO action_decision (id, request_id, verdict, provider, idempotency_key, decided_at, reason_code, policy_evidence_proto)
		VALUES ($1, $2, 'deny', 'test-provider', $3, now(), 'test_reason', $4)`,
		uuid.New(), id, "auto:"+id.String(), encoded)
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
			encoded := f.document(t, `SELECT policy_evidence_proto FROM action_decision WHERE request_id = $1`, written.ID)
			got, err := decodeStoredPolicyEvidenceProto(encoded)
			if err != nil || !reflect.DeepEqual(got, &evidence) {
				t.Errorf("typed policy evidence = %+v, %v; want %+v", got, err, evidence)
			}
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
		{"events", `SELECT count(*) FROM action_event WHERE actor_proto IS NULL`, 2},
		{"decision", `SELECT count(*) FROM action_decision
			WHERE decision_note IS NULL AND reason_code IS NULL AND reason_description IS NULL
				AND policy_evidence_proto IS NULL`, 1},
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
func (f fixture) insertInFlightAsPython(t *testing.T, key string, states []string, lease Lease, failure json.RawMessage) uuid.UUID {
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
	lost := f.insertInFlightAsPython(t, "python-lost", []string{"dispatching", "running"}, lostLease, nil)
	sweptLease := Lease{ExecutorID: "test-python-executor", Token: uuid.New()}
	pythonError := loadGolden(t).UnknownOutcomes[0].Error
	swept := f.insertInFlightAsPython(t, "python-swept", []string{"dispatching", "running", "execution_unknown"}, sweptLease, pythonError)

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
		if got := f.get(t, finished.id); got.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || got.Execution.ReconciledAt == nil {
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
	if err != nil || !dispatch || decided.State != action_servicepb.ActionState_ACTION_STATE_ALLOWED || decided.Version != 2 {
		t.Fatalf("decision = %+v, dispatch %v, %v", decided, dispatch, err)
	}
	cancelled := f.cancel(t, toCancel)
	if cancelled.Outcome != OutcomeCancelled || cancelled.Request.Version != 2 {
		t.Errorf("cancellation = %+v", cancelled)
	}
	for id, states := range map[uuid.UUID][]action_servicepb.ActionState{
		toDecide: {action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_ALLOWED},
		toCancel: {action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, action_servicepb.ActionState_ACTION_STATE_CANCELLED},
	} {
		if got := eventStates(f.events(t, id)); !slices.Equal(got, states) {
			t.Errorf("events of %s = %v, want %v", id, got, states)
		}
	}
}

func TestTypedReadersRejectInvalidPresentProtobuf(t *testing.T) {
	f := newFixture(t)
	actorRequest := f.submit(t, caller, "invalid-actor-proto")
	f.exec(t, `UPDATE action_event SET actor_proto = $3 WHERE request_id = $1 AND sequence = $2`,
		actorRequest.ID, 1, []byte{0x78, 0x01})
	if _, err := f.Events(t.Context(), actorRequest.ID, operator, EventsFilter{}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("an actor protobuf without a principal: %v", err)
	}
	f.exec(t, `UPDATE action_event SET actor_proto = decode('', 'hex') WHERE request_id = $1 AND sequence = 1`, actorRequest.ID)
	if f.count(t, `SELECT count(*) FROM action_event WHERE request_id = $1 AND sequence = 1
		AND actor_proto IS NOT NULL AND octet_length(actor_proto) = 0`, actorRequest.ID) != 1 {
		t.Fatal("fixture did not store an empty, non-NULL actor protobuf")
	}
	if _, err := f.Events(t.Context(), actorRequest.ID, operator, EventsFilter{}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("an empty actor protobuf: %v", err)
	}

	evidenceRequest := f.allowed(t, caller, "invalid-evidence-proto")
	unknownEvidence, err := proto.Marshal(&action_servicepb.PolicyEvidence{Matched: &action_servicepb.MatchedPolicy{
		Source: action_servicepb.PolicySource(99), Type: action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE action_decision SET policy_evidence_proto = $2 WHERE request_id = $1`, evidenceRequest.ID, unknownEvidence)
	if _, err := f.Get(t.Context(), evidenceRequest.ID, operator); err == nil {
		t.Fatal("an unknown policy-source enum was accepted")
	}
	f.exec(t, `UPDATE action_decision SET policy_evidence_proto = decode('', 'hex') WHERE request_id = $1`, evidenceRequest.ID)
	if f.count(t, `SELECT count(*) FROM action_decision WHERE request_id = $1
		AND policy_evidence_proto IS NOT NULL AND octet_length(policy_evidence_proto) = 0`, evidenceRequest.ID) != 1 {
		t.Fatal("fixture did not store an empty, non-NULL policy evidence protobuf")
	}
	if _, err := f.Get(t.Context(), evidenceRequest.ID, operator); err == nil {
		t.Fatal("an empty policy-evidence protobuf was accepted")
	}
}
