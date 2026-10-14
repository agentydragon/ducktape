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
	// RFC 3339 time the Sandbox became idle; absent while it is busy or the runner does not track it.
	IdleSinceAnnotation = "agentplane.allegedly.works/idle-since"
	// `running`, `succeeded`, `failed` or `interrupted`; absent when no bootstrap is configured or
	// none has been reported.
	BootstrapAnnotation = "agentplane.allegedly.works/bootstrap"
	// `true` while some Session's setup script runs; absent otherwise.
	SetupsRunningAnnotation = "agentplane.allegedly.works/setups-running"
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
	// Nil while the Sandbox is busy or the runner does not track activity.
	IdleSince *time.Time
	Bootstrap BootstrapState
	// Some Session's setup script is running.
	SetupsRunning bool
}

// readRunnerFacts reads the facts the Lease holds for the incarnation with this UID. A Lease whose
// `pod-uid` is another incarnation's holds none of its facts.
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
	facts.SetupsRunning = annotations[SetupsRunningAnnotation] == "true"
	if since, ok := annotations[IdleSinceAnnotation]; ok {
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
		PodUIDAnnotation, QuiesceStageAnnotation, IdleSinceAnnotation, BootstrapAnnotation, SetupsRunningAnnotation,
	} {
		if previous.Annotations[key] != current.Annotations[key] {
			return true
		}
	}
	return false
}
