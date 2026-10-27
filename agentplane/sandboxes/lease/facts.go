// Package lease owns the shared Lease protocol between the Runner Gateway and Sandbox Controller.
package lease

import (
	"encoding/json"
	"fmt"
	"time"

	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

const (
	RunnerLeaseSuffix             = "-runner"
	ConnectionLabel               = "agentplane.allegedly.works/runner-connection"
	ConnectionEpochAnnotation     = "agentplane.allegedly.works/connection-epoch"
	PodUIDAnnotation              = "agentplane.allegedly.works/pod-uid"
	RunnerBootIDAnnotation        = "agentplane.allegedly.works/runner-boot-id"
	QuiesceStageAnnotation        = "agentplane.allegedly.works/quiesce-stage"
	IdleSinceAnnotation           = "agentplane.allegedly.works/idle-since"
	BootstrapAnnotation           = "agentplane.allegedly.works/bootstrap"
	SetupsRunningAnnotation       = "agentplane.allegedly.works/setups-running"
	AutosuspendBlockersAnnotation = "agentplane.allegedly.works/autosuspend-blockers"
)

// QuiesceStage is the quiesce progress a runner publishes on its connection Lease.
type QuiesceStage uint8

const (
	QuiesceNone QuiesceStage = iota
	QuiesceDraining
	QuiesceFlushing
	QuiesceQuiesced
	// QuiesceStageUnrecognised is a value published by a newer Gateway.
	QuiesceStageUnrecognised
)

// BootstrapState is the bootstrap progress a runner publishes on its connection Lease.
type BootstrapState uint8

const (
	BootstrapNone BootstrapState = iota
	BootstrapRunning
	BootstrapSucceeded
	BootstrapFailed
	BootstrapInterrupted
	// BootstrapStateUnrecognised is a value published by a newer Gateway.
	BootstrapStateUnrecognised
)

// RunnerFacts are the per-incarnation facts a runner publishes on its connection Lease.
type RunnerFacts struct {
	Stage               QuiesceStage
	IdleSince           *time.Time
	Bootstrap           BootstrapState
	SetupsRunning       bool
	AutosuspendBlockers []sandboxapi.AutosuspendBlocker
}

// FromProto converts RunnerControl's facts into the Lease protocol. Unknown enum values are errors
// at the writer boundary; the decoder below names unknown values from a newer Gateway.
func FromProto(message *runnercontrolpb.RunnerFacts) (RunnerFacts, error) {
	var facts RunnerFacts
	switch message.GetQuiesceStage() {
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_UNSPECIFIED:
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_DRAINING:
		facts.Stage = QuiesceDraining
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_FLUSHING:
		facts.Stage = QuiesceFlushing
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_QUIESCED:
		facts.Stage = QuiesceQuiesced
	default:
		return RunnerFacts{}, fmt.Errorf("unknown runner quiesce stage %d", message.GetQuiesceStage())
	}
	switch message.GetBootstrap() {
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_UNSPECIFIED:
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_RUNNING:
		facts.Bootstrap = BootstrapRunning
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_SUCCEEDED:
		facts.Bootstrap = BootstrapSucceeded
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_FAILED:
		facts.Bootstrap = BootstrapFailed
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_INTERRUPTED:
		facts.Bootstrap = BootstrapInterrupted
	default:
		return RunnerFacts{}, fmt.Errorf("unknown runner bootstrap state %d", message.GetBootstrap())
	}
	facts.SetupsRunning = message.GetSetupsRunning()
	if timestamp := message.GetIdleSince(); timestamp != nil {
		if err := timestamp.CheckValid(); err != nil {
			return RunnerFacts{}, err
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
		return RunnerFacts{}, err
	}
	return facts, nil
}

// Annotations encodes facts into the Lease annotation values owned by a runner connection.
func (f RunnerFacts) Annotations() (map[string]string, error) {
	annotations := make(map[string]string)
	switch f.Stage {
	case QuiesceNone:
	case QuiesceDraining:
		annotations[QuiesceStageAnnotation] = "draining"
	case QuiesceFlushing:
		annotations[QuiesceStageAnnotation] = "flushing"
	case QuiesceQuiesced:
		annotations[QuiesceStageAnnotation] = "quiesced"
	default:
		return nil, fmt.Errorf("unknown runner quiesce stage %d", f.Stage)
	}
	switch f.Bootstrap {
	case BootstrapNone:
	case BootstrapRunning:
		annotations[BootstrapAnnotation] = "running"
	case BootstrapSucceeded:
		annotations[BootstrapAnnotation] = "succeeded"
	case BootstrapFailed:
		annotations[BootstrapAnnotation] = "failed"
	case BootstrapInterrupted:
		annotations[BootstrapAnnotation] = "interrupted"
	default:
		return nil, fmt.Errorf("unknown runner bootstrap state %d", f.Bootstrap)
	}
	if f.IdleSince != nil {
		annotations[IdleSinceAnnotation] = f.IdleSince.UTC().Format(time.RFC3339Nano)
	}
	if f.SetupsRunning {
		annotations[SetupsRunningAnnotation] = "true"
	}
	if len(f.AutosuspendBlockers) > 0 {
		if err := sandboxapi.ValidateAutosuspendBlockers(f.AutosuspendBlockers); err != nil {
			return nil, err
		}
		blockers, err := json.Marshal(f.AutosuspendBlockers)
		if err != nil {
			return nil, fmt.Errorf("encode autosuspend blockers: %w", err)
		}
		annotations[AutosuspendBlockersAnnotation] = string(blockers)
	}
	return annotations, nil
}

// DecodeFacts reads facts only when the Lease names the current runner incarnation.
func DecodeFacts(leaseName string, annotations map[string]string, incarnationUID string) (RunnerFacts, error) {
	var facts RunnerFacts
	if incarnationUID == "" || annotations[PodUIDAnnotation] != incarnationUID {
		return facts, nil
	}
	switch annotations[QuiesceStageAnnotation] {
	case "":
		facts.Stage = QuiesceNone
	case "draining":
		facts.Stage = QuiesceDraining
	case "flushing":
		facts.Stage = QuiesceFlushing
	case "quiesced":
		facts.Stage = QuiesceQuiesced
	default:
		facts.Stage = QuiesceStageUnrecognised
	}
	switch annotations[BootstrapAnnotation] {
	case "":
		facts.Bootstrap = BootstrapNone
	case "running":
		facts.Bootstrap = BootstrapRunning
	case "succeeded":
		facts.Bootstrap = BootstrapSucceeded
	case "failed":
		facts.Bootstrap = BootstrapFailed
	case "interrupted":
		facts.Bootstrap = BootstrapInterrupted
	default:
		facts.Bootstrap = BootstrapStateUnrecognised
	}
	facts.SetupsRunning = annotations[SetupsRunningAnnotation] == "true"
	if since, ok := annotations[IdleSinceAnnotation]; ok {
		idleSince, err := time.Parse(time.RFC3339, since)
		if err != nil {
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s: %w", leaseName, IdleSinceAnnotation, err)
		}
		facts.IdleSince = &idleSince
	}
	if encoded, ok := annotations[AutosuspendBlockersAnnotation]; ok {
		if err := json.Unmarshal([]byte(encoded), &facts.AutosuspendBlockers); err != nil {
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s: %w", leaseName, AutosuspendBlockersAnnotation, err)
		}
		if err := sandboxapi.ValidateAutosuspendBlockers(facts.AutosuspendBlockers); err != nil {
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s: %w", leaseName, AutosuspendBlockersAnnotation, err)
		}
	}
	return facts, nil
}

// FactsChanged reports whether an incarnation or one of its published facts changed. Lease renewals
// with the same annotations do not require a Sandbox reconciliation.
func FactsChanged(previous, current map[string]string) bool {
	for _, key := range []string{
		PodUIDAnnotation, QuiesceStageAnnotation, IdleSinceAnnotation, BootstrapAnnotation, SetupsRunningAnnotation,
		AutosuspendBlockersAnnotation,
	} {
		if previous[key] != current[key] {
			return true
		}
	}
	return false
}
