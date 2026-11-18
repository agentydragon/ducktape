package codemode

import (
	"context"
	"errors"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// Journal is bound to one sampler lease. Implementations fence every durable write.
// A receive is recorded before its reply or delegate can take effect.
type Journal interface {
	Epoch(context.Context) (int, error)
	StampHost(context.Context, string) error
	ProtocolEvent(context.Context, string, eventpb.ExecutionProgress_Phase, map[string]any) error
	CellStarted(context.Context, string) error
}

// DelegateHandler returns the wire reply and its durable evidence. An operational error
// fails the binding; uncertain tool effects must not be turned into successful replies.
type DelegateHandler func(context.Context, *Session, Delegate) (DelegateResult, DelegateResult, error)

var ErrHostInstanceChanged = errors.New("code-mode host instance changed before session/open")
