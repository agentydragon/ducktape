package sandboxcontroller

import (
	"context"
	"sync"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// idleTimers holds, per Sandbox, when this controller first saw its runner idle since the instant the runner
// publishes. Elapsed time is measured on the controller's clock from that first sight, so a skewed runner
// clock cannot suspend a Sandbox early; a restarted controller starts every timer over.
type idleTimers struct {
	mu        sync.Mutex
	sightings map[types.NamespacedName]idleSighting
}

type idleSighting struct {
	// The runner's `idle-since` fact: a different value is a new idle period.
	idleSince time.Time
	firstSeen time.Time
	// No suspend is attempted before this, after an attempt that did not take.
	notBefore time.Time
}

// observe is the sighting of the idle period that began at idleSince, started at now if it is new.
func (t *idleTimers) observe(key types.NamespacedName, idleSince, now time.Time) idleSighting {
	t.mu.Lock()
	defer t.mu.Unlock()
	sighting, ok := t.sightings[key]
	if !ok || !sighting.idleSince.Equal(idleSince) {
		sighting = idleSighting{idleSince: idleSince, firstSeen: now}
		if t.sightings == nil {
			t.sightings = map[types.NamespacedName]idleSighting{}
		}
		t.sightings[key] = sighting
	}
	return sighting
}

func (t *idleTimers) holdUntil(key types.NamespacedName, until time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if sighting, ok := t.sightings[key]; ok {
		sighting.notBefore = until
		t.sightings[key] = sighting
	}
}

func (t *idleTimers) forget(key types.NamespacedName) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sightings, key)
}

// idleCheck is what the runner's facts say about stopping a Sandbox that is to run.
type idleCheck struct {
	// The runner has entered lameduck, so it refuses every Session, though nothing records why: the
	// controller's suspension write failed, or it restarted before making one. The Sandbox takes the stop
	// path, which seals the incarnation and, as it is to run, replaces it.
	lameduck bool
	// When the idle timer next matters; zero where no event will end the wait.
	recheck time.Duration
	// The next attempt to suspend this idle Sandbox, absent when no attempt is scheduled.
	suspendAt *metav1.Time
}

// suspendIfIdle suspends a Sandbox that is to run once its runner has been idle for `spec.idleSuspendAfter`. It
// asks the runner to quiesce only if it is still idle since the instant the timer ran on, so work that arrives
// after the decision makes the runner refuse instead of draining a Sandbox with new work. Once the runner accepts,
// the suspension is set on the in-memory status for the write that follows, at the wake generation read before the
// call: a wake that lands meanwhile fails that write on the stale resource version, and the next reconcile finds
// the runner in lameduck, which it replaces rather than leaving the wake unanswered.
func (r *Reconciler) suspendIfIdle(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (idleCheck, error) {
	key := client.ObjectKeyFromObject(sandbox)
	pod := w.incarnation()
	if pod == nil {
		r.idle.forget(key)
		return idleCheck{}, nil
	}
	lease, err := r.runnerLease(ctx, sandbox)
	if err != nil {
		return idleCheck{}, err
	}
	facts, err := sandboxlease.DecodeFacts(lease.Name, lease.Annotations, string(pod.UID))
	if err != nil {
		return idleCheck{}, err
	}
	if facts.Stage != sandboxlease.QuiesceNone {
		r.idle.forget(key)
		return idleCheck{lameduck: true}, nil
	}
	if facts.IdleSince == nil {
		r.idle.forget(key)
		return idleCheck{}, nil
	}
	now := r.Now()
	sighting := r.idle.observe(key, *facts.IdleSince, now)
	if sandbox.Spec.IdleSuspendAfter == nil {
		return idleCheck{}, nil
	}
	after := sandbox.Spec.IdleSuspendAfter.Duration
	due := idleSuspendDeadline(sighting, after)
	if remaining := due.Sub(now); remaining > 0 {
		deadline := idleSuspendStatusTime(due)
		return idleCheck{recheck: remaining, suspendAt: &deadline}, nil
	}
	logger := ctrl.LoggerFrom(ctx)
	result, err := r.Quiescer.Quiesce(ctx, sandbox, facts.IdleSince)
	switch {
	case status.Code(err) == codes.Aborted:
		// The runner is busy again, or idle since another instant: its next fact, which enqueues the
		// Sandbox, says which. Otherwise the timer starts over.
		notBefore := now.Add(after)
		r.idle.holdUntil(key, notBefore)
		deadline := idleSuspendStatusTime(notBefore)
		return idleCheck{recheck: after, suspendAt: &deadline}, nil
	case err != nil:
		logger.Error(err, "asking an idle runner to quiesce", "sandbox", key.String())
		notBefore := now.Add(quiesceDeadline)
		r.idle.holdUntil(key, notBefore)
		deadline := idleSuspendStatusTime(notBefore)
		return idleCheck{recheck: quiesceDeadline, suspendAt: &deadline}, nil
	}
	logger.Info("suspending an idle Sandbox", "sandbox", key.String(), "idleSince", facts.IdleSince, "idleSuspendAfter", after.String())
	idle, generation := sandboxapi.SuspendedByIdle, sandbox.Spec.WakeGeneration
	sandbox.Status.SuspendedBy, sandbox.Status.SuspendedAtWakeGeneration = &idle, &generation
	// The accepted request is the quiesce's first attempt, so the stop path does not repeat it at once.
	runner := string(pod.UID)
	if result.Quiescing {
		sandbox.Status.QuiesceAttempt = &sandboxapi.QuiesceAttempt{RunnerPodUID: runner, AttemptedAt: metav1.NewTime(now)}
	} else {
		sandbox.Status.QuiescedIncarnation = &runner
	}
	return idleCheck{}, nil
}

// idleSuspendDeadline is the next attempt, after both the full idle period and any retry hold.
func idleSuspendDeadline(sighting idleSighting, after time.Duration) time.Time {
	due := sighting.firstSeen.Add(after)
	if sighting.notBefore.After(due) {
		return sighting.notBefore
	}
	return due
}

// idleSuspendStatusTime uses Kubernetes' whole-second wire precision so a readback compares equal.
func idleSuspendStatusTime(at time.Time) metav1.Time {
	return metav1.NewTime(at.Truncate(time.Second))
}
