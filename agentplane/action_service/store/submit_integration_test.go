package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func conflictReason(err error) ConflictReason {
	var conflict *ConflictError
	if errors.As(err, &conflict) {
		return conflict.Reason
	}
	return 0
}

func TestSubmitStoresAPendingRequestWithItsFirstEvent(t *testing.T) {
	f := newFixture(t)
	body := input("pending")
	body.Arguments = json.RawMessage(`{"text": "hello", "nested": {"n": [1, 2.50, null]}}`)
	description := "why it is wanted"
	body.Description = &description
	id := uuid.New()

	request := f.submitInput(t, caller, body, Admission{RequestID: id})

	if request.ID != id || request.IdempotencyKey != "pending" || request.Action != body.Action ||
		request.Title != body.Title || *request.Description != description || request.Caller != caller.Account ||
		request.State != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING || request.Version != 1 ||
		!request.CreatedAt.Equal(request.UpdatedAt) || request.Decision != nil || request.Execution != nil {
		t.Errorf("stored %+v", request)
	}
	sameJSON(t, request.Arguments, body.Arguments)
	var actionGroup, actionName string
	if err := f.db.QueryRowContext(t.Context(), `SELECT action_group, action_name FROM action_request WHERE id = $1`, id).
		Scan(&actionGroup, &actionName); err != nil {
		t.Fatal(err)
	}
	if actionGroup != body.Action.Group || actionName != body.Action.Name {
		t.Errorf("typed action identity = %q/%q, want %q/%q", actionGroup, actionName, body.Action.Group, body.Action.Name)
	}
	if got := f.get(t, id); !reflect.DeepEqual(got, request) {
		t.Errorf("read back %+v, want %+v", got, request)
	}
	events := f.events(t, id)
	want := []Event{{Sequence: 1, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, At: request.CreatedAt}}
	if !reflect.DeepEqual(events, want) {
		t.Errorf("events = %+v, want %+v", events, want)
	}
	if f.count(t, `SELECT count(*) FROM action_request WHERE history_at IS NOT NULL`) != 0 {
		t.Error("a pending request is in history")
	}
}

func TestAnIdempotencyKeyIsSpentWhateverTheEnvelopeSays(t *testing.T) {
	f := newFixture(t)
	first := f.submit(t, caller, "spent")

	reworded := input("spent")
	reworded.Title = "test title reworded on retry"
	_, err := f.Submit(t.Context(), caller, reworded, Admission{RequestID: uuid.New()})
	if conflictReason(err) != ConflictIdempotencyKeyUsed {
		t.Fatalf("resubmitting the key: %v", err)
	}
	listed, err := f.List(t.Context(), caller, ListFilter{IdempotencyKey: &first.IdempotencyKey})
	if err != nil || len(listed) != 1 || listed[0].ID != first.ID {
		t.Errorf("reading the request back by key = %+v, %v", listed, err)
	}

	// The key belongs to the caller, and a cancelled request keeps it.
	if other := f.submit(t, otherCaller, "spent"); other.ID == first.ID {
		t.Error("another caller reached the first caller's request")
	}
	if _, err := f.Cancel(t.Context(), first.ID, caller); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Submit(t.Context(), caller, input("spent"), Admission{RequestID: uuid.New()}); conflictReason(err) != ConflictIdempotencyKeyUsed {
		t.Errorf("resubmitting after a cancellation: %v", err)
	}
	if fresh := f.submit(t, caller, "fresh"); fresh.State != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING {
		t.Errorf("a fresh key started in %s", fresh.State)
	}
}

func TestADecisiveVoteCommitsItsDecisionWithTheInsert(t *testing.T) {
	evidence := &PolicyEvidence{
		Bindings:   []BindingEvidence{{Namespace: "test-namespace", Name: "test-binding", ResourceVersion: "7"}},
		PolicySets: []PolicySetEvidence{{Namespace: "test-namespace", Name: "test-set", Generation: 4}},
		Matched:    MatchedPolicy{Namespace: "test-namespace", PolicySet: "test-set", Source: PolicySourceRules, Type: PolicyArgumentSchema},
	}
	description := "matched binding test-binding"
	for _, test := range []struct {
		verdict   Verdict
		state     action_servicepb.ActionState
		execution bool
	}{
		{VerdictAllow, action_servicepb.ActionState_ACTION_STATE_ALLOWED, true},
		{VerdictDeny, action_servicepb.ActionState_ACTION_STATE_DENIED, false},
	} {
		t.Run(string(test.verdict), func(t *testing.T) {
			f := newFixture(t)
			request := f.submitInput(t, caller, input("voted"), Admission{RequestID: uuid.New(), Vote: &ProviderVote{
				Provider: "test-provider", Verdict: test.verdict, ReasonCode: "test_reason", ReasonDescription: &description,
				Evidence: evidence,
			}})

			if request.State != test.state || request.Version != 2 {
				t.Errorf("state %s version %d, want %s version 2", request.State, request.Version, test.state)
			}
			decision := request.Decision
			if decision == nil || decision.Verdict != test.verdict || decision.Provider != "test-provider" ||
				decision.Operator != nil || decision.DecisionNote != nil || *decision.ReasonCode != "test_reason" ||
				*decision.ReasonDescription != description || !reflect.DeepEqual(decision.PolicyEvidence, evidence) ||
				decision.IdempotencyKey != "auto:"+request.ID.String() || !decision.DecidedAt.Equal(request.UpdatedAt) {
				t.Errorf("decision %+v", decision)
			}
			var encodedEvidence []byte
			if err := f.db.QueryRowContext(t.Context(), `SELECT policy_evidence_proto FROM action_decision WHERE request_id = $1`,
				request.ID).Scan(&encodedEvidence); err != nil {
				t.Fatal(err)
			}
			var storedEvidence action_servicepb.PolicyEvidence
			if err := proto.Unmarshal(encodedEvidence, &storedEvidence); err != nil {
				t.Fatal(err)
			}
			wantEvidence := &action_servicepb.PolicyEvidence{
				Bindings:   []*action_servicepb.BindingEvidence{{Namespace: "test-namespace", Name: "test-binding", ResourceVersion: "7"}},
				PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: "test-namespace", Name: "test-set", Generation: 4}},
				Matched: &action_servicepb.MatchedPolicy{
					Namespace: "test-namespace", PolicySet: "test-set",
					Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
					Type:   action_servicepb.PolicyKind_POLICY_KIND_ARGUMENT_SCHEMA,
				},
			}
			if !proto.Equal(&storedEvidence, wantEvidence) {
				t.Errorf("protobuf evidence = %v, want %v", &storedEvidence, wantEvidence)
			}
			if (request.Execution != nil) != test.execution {
				t.Fatalf("execution = %+v, want one: %v", request.Execution, test.execution)
			}
			if test.execution {
				execution := request.Execution
				if execution.State != ExecutionPendingDispatch || execution.Result != nil || execution.Error != nil ||
					execution.StartedAt != nil || execution.CompletedAt != nil || execution.ReconciledAt != nil ||
					!execution.CreatedAt.Equal(request.UpdatedAt) {
					t.Errorf("execution %+v", execution)
				}
			}
			events := f.events(t, request.ID)
			if got := eventStates(events); !reflect.DeepEqual(got, []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, test.state}) ||
				!events[0].At.Equal(events[1].At) || events[1].Sequence != 2 {
				t.Errorf("events %+v", events)
			}
			if f.count(t, `SELECT count(*) FROM action_request WHERE history_at = updated_at`) != 1 {
				t.Error("the request did not enter history when it was decided")
			}
		})
	}
}

func TestAnInvalidSubmissionStoresNothing(t *testing.T) {
	f := newFixture(t)
	body := input("invalid")
	body.Title = ""
	if _, err := f.Submit(t.Context(), caller, body, Admission{RequestID: uuid.New()}); !errors.Is(err, ErrInvalid) {
		t.Errorf("an empty title: %v", err)
	}
	vote := &ProviderVote{Provider: "test-provider", Verdict: "no_opinion", ReasonCode: "test_reason"}
	if _, err := f.Submit(t.Context(), caller, input("invalid"), Admission{RequestID: uuid.New(), Vote: vote}); !errors.Is(err, ErrInvalid) {
		t.Errorf("a vote that does not decide: %v", err)
	}
	if f.count(t, `SELECT count(*) FROM action_request`) != 0 {
		t.Error("an invalid submission was stored")
	}
}

func TestSubmissionsOfOneKeyHaveOneWinner(t *testing.T) {
	for _, test := range []struct {
		name   string
		finish func(release func(), rollback func())
		want   func(error) bool
	}{
		{"the other commits first", func(release func(), _ func()) { release() }, func(err error) bool { return conflictReason(err) == ConflictIdempotencyKeyUsed }},
		{"the other rolls back", func(_ func(), rollback func()) { rollback() }, func(err error) bool { return err == nil }},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newFixture(t)
			other, err := f.db.BeginTx(t.Context(), nil)
			if err != nil {
				t.Fatal(err)
			}
			defer other.Rollback()
			// The competing insert is in flight and uncommitted: the submission queues behind its key.
			if _, err := other.ExecContext(t.Context(), `
				INSERT INTO action_request (id, idempotency_key, action_group, action_name, arguments, title, caller_namespace, caller_name,
					state, version, created_at, updated_at)
				VALUES ($1, 'contended', 'test-group', 'test-action', '{}', 'test title', $2, $3,
					'decision_pending', 1, now(), now())`,
				uuid.New(), caller.Account.Namespace, caller.Account.Name); err != nil {
				t.Fatal(err)
			}

			submitted := async(func() error {
				_, err := f.Submit(t.Context(), caller, input("contended"), Admission{RequestID: uuid.New()})
				return err
			})
			f.awaitBlocked(t, 1)
			test.finish(func() { other.Commit() }, func() { other.Rollback() })
			if err := await(t, submitted); !test.want(err) {
				t.Errorf("submission = %v", err)
			}
		})
	}
}
