package sandboxcontroller

import (
	"fmt"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
)

const (
	// The facts below are valid only while the Lease's `pod-uid` is the incarnation's: a new
	// connection rewrites the whole set, so a different `pod-uid` means they describe another runner.
	// `draining`, `flushing` or `quiesced`; absent while no quiesce is under way.
	QuiesceStageAnnotation = "agentplane.allegedly.works/quiesce-stage"
	// `true` while any Session is busy or a background process or attached terminal runs, else `false`.
	BusyAnnotation = "agentplane.allegedly.works/busy"
	// RFC 3339 time the Sandbox became idle; present only with `busy` `false`.
	IdleSinceAnnotation = "agentplane.allegedly.works/idle-since"
	// `running`, `succeeded`, `failed` or `interrupted`; absent when no bootstrap is configured or
	// none has been reported.
	BootstrapAnnotation = "agentplane.allegedly.works/bootstrap"
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

// BootstrapState is how far the Sandbox bootstrap has got.
type BootstrapState int

const (
	BootstrapNone BootstrapState = iota
	BootstrapRunning
	BootstrapSucceeded
	BootstrapFailed
	BootstrapInterrupted
	// A state a newer runner publishes that this controller does not know.
	BootstrapStateUnrecognised
)

// RunnerFacts is what the runner has published about its incarnation on the connection Lease.
type RunnerFacts struct {
	Stage QuiesceStage
	// Nil until the runner has reported its activity.
	Busy *bool
	// Set only when Busy is false.
	IdleSince *time.Time
	Bootstrap BootstrapState
}

// readRunnerFacts reads the facts the Lease holds for the incarnation with this UID. A Lease whose
// `pod-uid` is another incarnation's holds none of its facts.
//
// Staged for the quiesce progress check, which reads Stage here instead of repeating the Quiesce
// call; delete if that change is abandoned.
func readRunnerFacts(lease *coordinationv1.Lease, incarnationUID string) (RunnerFacts, error) {
	var facts RunnerFacts
	annotations := lease.Annotations
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
		PodUIDAnnotation, QuiesceStageAnnotation, BusyAnnotation, IdleSinceAnnotation, BootstrapAnnotation,
	} {
		if previous.Annotations[key] != current.Annotations[key] {
			return true
		}
	}
	return false
}
