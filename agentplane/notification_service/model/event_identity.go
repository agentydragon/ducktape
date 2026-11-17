package model

import "github.com/google/uuid"

// ActionsEvent identifies one Action event by its request and durable sequence.
type ActionsEvent struct {
	RequestID uuid.UUID
	Sequence  int64
}

// MessageEvent identifies a message and the authenticated sender who submitted it.
type MessageEvent struct {
	MessageID      uuid.UUID
	Sender         ServiceAccount
	SenderIdentity *Identity
}
