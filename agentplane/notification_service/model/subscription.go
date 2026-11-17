package model

import (
	"errors"

	"github.com/google/uuid"
)

// ActionsSource follows the events of one Action request.
type ActionsSource struct {
	RequestID uuid.UUID
	// AfterSequence is the request's own event sequence to replay after, not an inbox cursor.
	AfterSequence int64
}

func (r DestinationRef) Validate() error {
	return errors.Join(
		CheckText("destination_ref.namespace", r.Namespace, 1, 63),
		CheckText("destination_ref.name", r.Name, 1, 63),
		CheckText("destination_ref.uid", r.UID, 1, 128),
	)
}
