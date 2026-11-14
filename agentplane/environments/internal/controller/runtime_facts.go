package sandboxcontroller

import (
	"fmt"
	"time"

	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

// QuiesceStage is directly observed Runtime quiesce progress.
type QuiesceStage uint8

const (
	QuiesceNone QuiesceStage = iota
	QuiesceDraining
	QuiesceFlushing
	QuiesceQuiesced
)

// BootstrapState is directly observed Runtime bootstrap progress.
type BootstrapState uint8

const (
	BootstrapNone BootstrapState = iota
	BootstrapRunning
	BootstrapSucceeded
	BootstrapFailed
	BootstrapInterrupted
)

// EnvironmentRuntimeFacts are the current incarnation's directly observed facts.
type EnvironmentRuntimeFacts struct {
	Stage               QuiesceStage
	IdleSince           *time.Time
	Bootstrap           BootstrapState
	SetupsRunning       bool
	AutosuspendBlockers []sandboxapi.AutosuspendBlocker
}

// runtimeFactsFromProto validates a direct Runtime snapshot at the observation boundary.
func runtimeFactsFromProto(message *environmentruntimestatepb.EnvironmentRuntimeFacts) (EnvironmentRuntimeFacts, error) {
	var facts EnvironmentRuntimeFacts
	switch message.GetQuiesceStage() {
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_UNSPECIFIED:
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_DRAINING:
		facts.Stage = QuiesceDraining
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING:
		facts.Stage = QuiesceFlushing
	case environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED:
		facts.Stage = QuiesceQuiesced
	default:
		return EnvironmentRuntimeFacts{}, fmt.Errorf("unknown runner quiesce stage %d", message.GetQuiesceStage())
	}
	switch message.GetBootstrap() {
	case environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_UNSPECIFIED:
	case environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_RUNNING:
		facts.Bootstrap = BootstrapRunning
	case environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED:
		facts.Bootstrap = BootstrapSucceeded
	case environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED:
		facts.Bootstrap = BootstrapFailed
	case environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED:
		facts.Bootstrap = BootstrapInterrupted
	default:
		return EnvironmentRuntimeFacts{}, fmt.Errorf("unknown runner bootstrap state %d", message.GetBootstrap())
	}
	facts.SetupsRunning = message.GetSetupsRunning()
	if timestamp := message.GetIdleSince(); timestamp != nil {
		if err := timestamp.CheckValid(); err != nil {
			return EnvironmentRuntimeFacts{}, err
		}
		idleSince := timestamp.AsTime()
		facts.IdleSince = &idleSince
	}
	for _, blocker := range message.GetAutosuspendBlockers() {
		fact := sandboxapi.AutosuspendBlocker{Reason: sandboxapi.AutosuspendBlockerReason(blocker.GetReason())}
		if blocker.SessionId != nil {
			sessionID := blocker.GetSessionId()
			fact.SessionID = &sessionID
		}
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, fact)
	}
	if err := sandboxapi.ValidateAutosuspendBlockers(facts.AutosuspendBlockers); err != nil {
		return EnvironmentRuntimeFacts{}, err
	}
	return facts, nil
}
