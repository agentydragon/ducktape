package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// ErrActionHistoryGap is a read of an Action request's events that skips a sequence the subscription has not
// recorded.
var ErrActionHistoryGap = errors.New("action event history has a gap")

// actionsReadInterval is the wait before an Actions subscription is read again after a read that succeeded.
const actionsReadInterval = 5 * time.Second

// ActionEvent is an event of an Action request as the Action Service returns it, and the payload of the
// inbox entry it becomes.
type ActionEvent struct {
	// Sequence is the event's position in the request's own log, from 1.
	Sequence int64
	State    action_servicepb.ActionState
	At       time.Time
	// Actor is who cancelled the request, or nil for an event nobody caused.
	Actor *action_servicepb.Principal
}

// The JSON forms below are the ones the Action Service writes and the rows already in the database hold.

// Notification's stored event decoder accepts explicit nulls in the other actor variant's fields. Keep that wire
// policy local while sharing the resulting actor vocabulary with the Action Service.
func decodeActionActor(data json.RawMessage) (*action_servicepb.Principal, error) {
	if data == nil || string(data) == "null" {
		return nil, nil
	}
	var wire struct {
		Account *ServiceAccount `json:"account"`
		Issuer  *string         `json:"issuer"`
		Subject *string         `json:"subject"`
	}
	if err := decode(data, &wire); err != nil {
		return nil, err
	}
	switch {
	case wire.Account != nil && wire.Issuer == nil && wire.Subject == nil:
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: wire.Account.Proto()}}, nil
	case wire.Account == nil && wire.Issuer != nil && wire.Subject != nil && *wire.Issuer != "" && *wire.Subject != "":
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: *wire.Issuer, Subject: *wire.Subject}}}, nil
	default:
		return nil, errors.New("an actor is a caller (account) or an operator (issuer and subject)")
	}
}

func (e *ActionEvent) UnmarshalJSON(data []byte) error {
	var wire struct {
		Sequence *int64          `json:"sequence"`
		State    *string         `json:"state"`
		At       *string         `json:"at"`
		Actor    json.RawMessage `json:"actor"`
	}
	if err := decode(data, &wire); err != nil {
		return err
	}
	if err := errors.Join(
		required("sequence", wire.Sequence != nil), required("state", wire.State != nil), required("at", wire.At != nil),
	); err != nil {
		return err
	}
	state, known := actionStateFromStored(*wire.State)
	if !known {
		return fmt.Errorf("unknown Action state %q", *wire.State)
	}
	at, err := time.Parse(time.RFC3339Nano, *wire.At)
	if err != nil {
		return fmt.Errorf("at: %w", err)
	}
	actor, err := decodeActionActor(wire.Actor)
	if err != nil {
		return err
	}
	*e = ActionEvent{Sequence: *wire.Sequence, State: state, At: at.Truncate(time.Microsecond), Actor: actor}
	return nil
}

// MarshalJSON writes the payload of an Action entry: the event as Pydantic dumps it, with the time in the
// offset it came in, whole seconds when it has no microseconds and six digits when it has.
func (e ActionEvent) MarshalJSON() ([]byte, error) {
	layout := "2006-01-02T15:04:05"
	if e.At.Nanosecond() != 0 {
		layout += ".000000"
	}
	state, known := storedActionState(e.State)
	if !known {
		return nil, fmt.Errorf("unknown Action state %v", e.State)
	}
	actor, err := encodeActionActor(e.Actor)
	if err != nil {
		return nil, err
	}
	return json.Marshal(struct {
		Sequence int64           `json:"sequence"`
		State    string          `json:"state"`
		At       string          `json:"at"`
		Actor    json.RawMessage `json:"actor"`
	}{e.Sequence, state, e.At.Format(layout + "Z07:00"), actor})
}

// RecordActionEvents records what a read of the Action request an Actions subscription follows returned, in the
// claimed inbox and in one fenced transaction. Events at or before the sequence the subscription has recorded
// were recorded already and are skipped; the first of the others must follow that sequence directly, or nothing
// is recorded and the read fails with ErrActionHistoryGap.
//
// A subscription that was cancelled, expired or renewed since it was read as due (its version moved) is left
// as it is. Otherwise the read counts as a success: the failure shown on the subscription clears, and the
// request is read again in five seconds.
func (s *Store) RecordActionEvents(ctx context.Context, claim Claim, due DueSubscription, events []ActionEvent) error {
	source, ok := due.Source.(ActionsSource)
	if !ok {
		return fmt.Errorf("subscription %s does not follow an Action request", due.ID)
	}
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		recorded := now()
		var after sql.NullInt64
		err := tx.QueryRowContext(ctx, `
			UPDATE subscription SET next_attempt = $3, last_success_at = $4,
				error = NULL, error_kind = NULL, error_since = NULL, error_observed_at = NULL
			WHERE id = $1 AND inbox_id = $2 AND NOT cancelled AND version = $5 AND expires_at > $4
			RETURNING actions_after_sequence`,
			due.ID, held.ID, recorded.Add(actionsReadInterval), recorded, due.Version).Scan(&after)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if !after.Valid {
			return fmt.Errorf("subscription %s follows an Action request but has no recorded sequence", due.ID)
		}
		sequence := after.Int64
		for _, event := range events {
			if event.Sequence <= sequence {
				continue
			}
			if event.Sequence != sequence+1 {
				return ErrActionHistoryGap
			}
			payload, err := json.Marshal(event)
			if err != nil {
				return err
			}
			identity := ActionsEvent{RequestID: source.RequestID, Sequence: event.Sequence}
			if err := s.AppendEvent(ctx, tx, held.ID, due.ID, identity, payload); err != nil {
				return err
			}
			sequence = event.Sequence
		}
		if sequence != after.Int64 {
			if _, err := tx.ExecContext(ctx, `UPDATE subscription SET actions_after_sequence = $2 WHERE id = $1`,
				due.ID, sequence); err != nil {
				return err
			}
		}
		if len(events) > 0 {
			if _, err := tx.ExecContext(ctx, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, held.ID, recorded); err != nil {
				return err
			}
		}
		return notify(ctx, tx)
	})
}
