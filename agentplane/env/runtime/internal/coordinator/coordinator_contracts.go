package coordinator

import "context"

type runtimeTransitionKind uint8

const (
	runtimeAdopt runtimeTransitionKind = iota + 1
	runtimeQuiesce
)

type runtimeTransitionRequest struct {
	ctx             context.Context
	done            chan struct{}
	err             error
	Kind            runtimeTransitionKind
	UID             string
	HasIdleSince    bool
	IdleSinceMicros int64
}
