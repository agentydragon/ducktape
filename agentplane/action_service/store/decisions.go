package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

// HumanProvider is the provider a human Decision is recorded under.
const HumanProvider = "human_operator"

// decisionRecord is a Decision to record: a human's, which names its operator, or a provider's, which names its
// reason.
type decisionRecord struct {
	Verdict           Verdict
	Provider          string
	Operator          *OperatorPrincipal
	IdempotencyKey    string
	DecisionNote      *string
	ReasonCode        *string
	ReasonDescription *string
	PolicyEvidence    *PolicyEvidence
}

// recordDecision decides a pending request in the caller's transaction and reports whether it now awaits
// dispatch: an allow adds the one Execution and wakes the dispatchers.
func recordDecision(ctx context.Context, tx *sql.Tx, row *requestRow, at time.Time, decision decisionRecord) (bool, error) {
	var evidence, evidenceProto, issuer, subject any
	if decision.PolicyEvidence != nil {
		encoded, err := json.Marshal(decision.PolicyEvidence)
		if err != nil {
			return false, err
		}
		evidence = string(encoded)
		evidenceProto, err = encodePolicyEvidence(decision.PolicyEvidence)
		if err != nil {
			return false, err
		}
	}
	if decision.Operator != nil {
		issuer, subject = decision.Operator.Issuer, decision.Operator.Subject
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO action_decision (id, request_id, verdict, provider, operator_issuer, operator_subject,
			decision_note, reason_code, reason_description, policy_evidence, policy_evidence_proto, idempotency_key, decided_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13)`,
		uuid.New(), row.ID, decision.Verdict, decision.Provider, issuer, subject, decision.DecisionNote,
		decision.ReasonCode, decision.ReasonDescription, evidence, evidenceProto, decision.IdempotencyKey, at); err != nil {
		return false, err
	}
	state := action_servicepb.ActionState_ACTION_STATE_DENIED
	if decision.Verdict == VerdictAllow {
		state = action_servicepb.ActionState_ACTION_STATE_ALLOWED
	}
	if err := advance(ctx, tx, row, state, at, nil); err != nil {
		return false, err
	}
	if decision.Verdict != VerdictAllow {
		return false, nil
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO action_execution (id, request_id, state, created_at) VALUES ($1, $2, $3, $4)`,
		uuid.New(), row.ID, ExecutionPendingDispatch, at); err != nil {
		return false, err
	}
	return true, notify(ctx, tx, DispatchChannel, "")
}

// Decide records an operator's Decision on a pending request and returns the request and whether it now awaits
// dispatch.
//
// The Decision key is idempotent per operator: a replay of the same verdict returns the request as it stands and
// does not dispatch again. A first Decision must name the request's current version.
func (s *Store) Decide(ctx context.Context, id uuid.UUID, input DecisionInput, operator OperatorPrincipal) (ActionRequest, bool, error) {
	if err := input.Validate(); err != nil {
		return ActionRequest{}, false, err
	}
	if err := operator.validate(); err != nil {
		return ActionRequest{}, false, err
	}
	var view ActionRequest
	var dispatch bool
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		var priorRequest uuid.UUID
		var priorVerdict string
		err = tx.QueryRowContext(ctx, `
			SELECT request_id, verdict FROM action_decision
			WHERE provider = $1 AND operator_issuer = $2 AND operator_subject = $3 AND idempotency_key = $4`,
			HumanProvider, operator.Issuer, operator.Subject, input.IdempotencyKey).Scan(&priorRequest, &priorVerdict)
		switch {
		case err == nil:
			if priorRequest != id || Verdict(priorVerdict) != input.Verdict {
				return &ConflictError{
					Reason:  ConflictDecisionKeyReused,
					Message: "decision idempotency key was already used for another decision",
				}
			}
			views, err := attach(ctx, tx, []requestRow{row})
			if err != nil {
				return err
			}
			view = views[0]
			return nil
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}
		if row.Version != input.ExpectedVersion {
			return &ConflictError{
				Reason: ConflictVersionChanged,
				Message: fmt.Sprintf("request changed: expected version %d, current version %d",
					input.ExpectedVersion, row.Version),
			}
		}
		if row.State != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING {
			return &ConflictError{
				Reason:  ConflictAlreadyDecided,
				Message: fmt.Sprintf("request was already decided; current state is %s", storedState(row.State)),
			}
		}
		if dispatch, err = recordDecision(ctx, tx, &row, now(), decisionRecord{
			Verdict:        input.Verdict,
			Provider:       HumanProvider,
			Operator:       &operator,
			IdempotencyKey: input.IdempotencyKey,
			DecisionNote:   input.DecisionNote,
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
	return view, dispatch, err
}
