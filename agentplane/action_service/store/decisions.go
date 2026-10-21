package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// decisionRecord is a Decision to record: an operator's, which names them, or the caller's policies', which says why.
type decisionRecord struct {
	Verdict           Verdict
	Operator          *OperatorPrincipal
	DecisionNote      *string
	ReasonDescription *string
	PolicyEvidence    *PolicyEvidence
}

// CLEANUP(added 2026-10-21): delete these values, and the columns they fill, at the stage O schema drop. Until then
// Python reads action_decision, so the store writes what it expects of the columns Go no longer reads:
//   - provider: "human_operator" for an operator's Decision, "action_policy_set" for a policy's (NOT NULL);
//   - reason_code: "policy_set_auto_approve" for a policy's Decision, NULL for an operator's;
//   - idempotency_key: a fresh random UUID per Decision (NOT NULL, unique with provider and operator);
//   - policy_evidence.matched.source: "rules", which MatchedPolicy.MarshalJSON writes.
const (
	operatorProvider = "human_operator"
	policyProvider   = "action_policy_set"
	policyReasonCode = "policy_set_auto_approve"
)

// recordDecision decides a pending request in the caller's transaction: an allow adds the one Execution and wakes
// the dispatchers.
func recordDecision(ctx context.Context, tx *sql.Tx, row *requestRow, at time.Time, decision decisionRecord) error {
	var evidence, issuer, subject, reasonCode any
	if decision.PolicyEvidence != nil {
		encoded, err := json.Marshal(decision.PolicyEvidence)
		if err != nil {
			return err
		}
		evidence = string(encoded)
	}
	provider := policyProvider
	if decision.Operator != nil {
		provider, issuer, subject = operatorProvider, decision.Operator.Issuer, decision.Operator.Subject
	} else {
		reasonCode = policyReasonCode
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO action_decision (id, request_id, verdict, provider, operator_issuer, operator_subject,
			decision_note, reason_code, reason_description, policy_evidence, idempotency_key, decided_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12)`,
		uuid.New(), row.ID, decision.Verdict, provider, issuer, subject, decision.DecisionNote, reasonCode,
		decision.ReasonDescription, evidence, uuid.NewString(), at); err != nil {
		return err
	}
	state := StateDenied
	if decision.Verdict == VerdictAllow {
		state = StateAllowed
	}
	if err := advance(ctx, tx, row, state, at, nil); err != nil {
		return err
	}
	if decision.Verdict != VerdictAllow {
		return nil
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO action_execution (id, request_id, state, created_at) VALUES ($1, $2, $3, $4)`,
		uuid.New(), row.ID, ExecutionPendingDispatch, at); err != nil {
		return err
	}
	return notify(ctx, tx, DispatchChannel, "")
}

// Decide records an operator's Decision on a pending request and returns the request. The Decision must name the
// request's current version, so a repeated Decision finds the version it named gone.
func (s *Store) Decide(ctx context.Context, id uuid.UUID, input DecisionInput, operator OperatorPrincipal) (ActionRequest, error) {
	if err := input.Validate(); err != nil {
		return ActionRequest{}, err
	}
	if err := operator.validate(); err != nil {
		return ActionRequest{}, err
	}
	var view ActionRequest
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		if row.Version != input.ExpectedVersion {
			return &ConflictError{
				Reason: ConflictVersionChanged,
				Message: fmt.Sprintf("request changed: expected version %d, current version %d",
					input.ExpectedVersion, row.Version),
			}
		}
		if row.State != StateDecisionPending {
			return &ConflictError{
				Reason:  ConflictAlreadyDecided,
				Message: fmt.Sprintf("request was already decided; current state is %s", row.State),
			}
		}
		if err := recordDecision(ctx, tx, &row, now(), decisionRecord{
			Verdict: input.Verdict, Operator: &operator, DecisionNote: input.DecisionNote,
		}); err != nil {
			return err
		}
		views, err := attach(ctx, tx, []requestRow{row})
		if err != nil {
			return err
		}
		view = views[0]
		return nil
	})
	return view, err
}
