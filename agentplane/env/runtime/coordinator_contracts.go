package runtime

import "context"

// runtimeCloseStage is the Runtime's ordered shutdown barrier. ReadyToJoin means
// the COSI engine can stop; the caller still waits for worker joins and owner release.
type runtimeCloseStage uint8

const (
	runtimeClosePreparing runtimeCloseStage = iota
	runtimeCloseNativeStopping
	runtimeCloseExecutionFencing
	runtimeCloseHistoryFlushing
	runtimeCloseSessionsJoining
	runtimeCloseBootstrapSettling
	runtimeCloseReadyToJoin
	runtimeCloseFailed
)

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

type runtimeTransitionStepKind uint8

const (
	runtimeTransitionPlanning runtimeTransitionStepKind = iota
	runtimeTransitionNativeStopping
	runtimeTransitionExecutionStarting
	runtimeTransitionDesiredPublishing
	runtimeTransitionExecutionFencing
	runtimeTransitionHistoryAcknowledging
	runtimeTransitionIncarnationAdopting
	runtimeTransitionBootstrapRequesting
	runtimeTransitionQuiesceSealing
)
