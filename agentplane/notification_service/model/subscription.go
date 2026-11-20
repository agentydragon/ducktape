package model

import (
	"github.com/google/uuid"
)

// ActionsSource follows the events of one Action request.
type ActionsSource struct {
	RequestID uuid.UUID
	// AfterSequence is the request's own event sequence to replay after, not an inbox cursor.
	AfterSequence int64
}
