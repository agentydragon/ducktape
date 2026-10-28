package storagecodec

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

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
func DecodeActionActor(data json.RawMessage) (*action_servicepb.Principal, error) {
	if data == nil || string(data) == "null" {
		return nil, nil
	}
	var wire struct {
		Account *actionActorAccount `json:"account"`
		Issuer  *string             `json:"issuer"`
		Subject *string             `json:"subject"`
	}
	if err := Decode(data, &wire); err != nil {
		return nil, err
	}
	switch {
	case wire.Account != nil && wire.Issuer == nil && wire.Subject == nil:
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: ServiceAccountProto(model.ServiceAccount{
			Namespace: wire.Account.Namespace,
			Name:      wire.Account.Name,
		})}}, nil
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
	if err := Decode(data, &wire); err != nil {
		return err
	}
	if err := errors.Join(
		Required("sequence", wire.Sequence != nil), Required("state", wire.State != nil), Required("at", wire.At != nil),
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
	actor, err := DecodeActionActor(wire.Actor)
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
	actor, err := EncodeActionActor(e.Actor)
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
