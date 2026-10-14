package sandboxcontroller

import (
	"fmt"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
)

const (
	// On the Runner Gateway's Lease for a Sandbox: the incarnation UID the facts below describe. Written
	// in the same update as any of them; facts stamped with another incarnation's UID, or unstamped,
	// are not read.
	RunnerFactsUIDAnnotation = "agentplane.allegedly.works/runner-facts-uid"
	// `draining`, `flushing` or `quiesced`; absent while no quiesce is under way.
	QuiesceStageAnnotation = "agentplane.allegedly.works/quiesce-stage"
	// `true` while any Session is busy or a background process or attached terminal runs, else `false`.
	BusyAnnotation = "agentplane.allegedly.works/busy"
	// RFC 3339 time the Sandbox became idle; present only with `busy` `false`.
	IdleSinceAnnotation = "agentplane.allegedly.works/idle-since"
	// `succeeded` or `failed`; absent until the Sandbox bootstrap has finished.
	BootstrapResultAnnotation = "agentplane.allegedly.works/bootstrap-result"
)

// QuiesceStage is how far the runner has got in quiescing.
type QuiesceStage int

const (
	QuiesceNone QuiesceStage = iota
	QuiesceDraining
	QuiesceFlushing
	QuiesceQuiesced
	// A stage a newer runner publishes that this controller does not know. Consumers dispatch on it
	// explicitly; it is neither "none" nor any known stage.
	QuiesceStageUnrecognised
)

// BootstrapResult is how the Sandbox bootstrap ended.
type BootstrapResult int

const (
	BootstrapPending BootstrapResult = iota
	BootstrapSucceeded
	BootstrapFailed
	// A result a newer runner publishes that this controller does not know.
	BootstrapResultUnrecognised
)

// RunnerFacts is what the runner has published about its incarnation on the connection Lease.
type RunnerFacts struct {
	Stage QuiesceStage
	// Nil until the runner has reported its activity.
	Busy *bool
	// Set only when Busy is false.
	IdleSince *time.Time
	Bootstrap BootstrapResult
}

// readRunnerFacts reads the facts the Lease holds for the incarnation with this UID. A Lease stamped
// for another incarnation holds none of its facts.
//
// Staged for the quiesce progress check, which reads Stage here instead of repeating the Quiesce
// call; delete if that change is abandoned.
func readRunnerFacts(lease *coordinationv1.Lease, incarnationUID string) (RunnerFacts, error) {
	var facts RunnerFacts
	annotations := lease.Annotations
	if incarnationUID == "" || annotations[RunnerFactsUIDAnnotation] != incarnationUID {
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
	switch annotations[BootstrapResultAnnotation] {
	case "":
		facts.Bootstrap = BootstrapPending
	case "succeeded":
		facts.Bootstrap = BootstrapSucceeded
	case "failed":
		facts.Bootstrap = BootstrapFailed
	default:
		facts.Bootstrap = BootstrapResultUnrecognised
	}
	if busy, ok := annotations[BusyAnnotation]; ok {
		switch busy {
		case "true":
			facts.Busy = new(bool)
			*facts.Busy = true
		case "false":
			facts.Busy = new(bool)
		default:
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s is %q, want true or false", lease.Name, BusyAnnotation, busy)
		}
	}
	if since, ok := annotations[IdleSinceAnnotation]; ok {
		if facts.Busy == nil || *facts.Busy {
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s without %s=false", lease.Name, IdleSinceAnnotation, BusyAnnotation)
		}
		idleSince, err := time.Parse(time.RFC3339, since)
		if err != nil {
			return RunnerFacts{}, fmt.Errorf("lease %s: annotation %s: %w", lease.Name, IdleSinceAnnotation, err)
		}
		facts.IdleSince = &idleSince
	}
	return facts, nil
}

// runnerFactsChanged is whether any fact annotation differs between two versions of a Lease. Renewals
// carry the same facts and are not changes.
func runnerFactsChanged(previous, current *coordinationv1.Lease) bool {
	for _, key := range []string{
		RunnerFactsUIDAnnotation, QuiesceStageAnnotation, BusyAnnotation, IdleSinceAnnotation, BootstrapResultAnnotation,
	} {
		if previous.Annotations[key] != current.Annotations[key] {
			return true
		}
	}
	return false
}
