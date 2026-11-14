package sandboxcontroller

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

type runtimeStateWatchAttempt struct {
	ctx          context.Context
	sandbox      *sandboxapi.Sandbox
	pod          *corev1.Pod
	credentialID string
	publish      func(*environmentruntimepb.WatchStateResponse) error
	finish       chan error
}

type controlledRuntimeWatcher struct {
	calls chan *runtimeStateWatchAttempt
}

func (w *controlledRuntimeWatcher) Watch(ctx context.Context, sandbox *sandboxapi.Sandbox, pod *corev1.Pod, credentialID string, publish func(*environmentruntimepb.WatchStateResponse) error) error {
	attempt := &runtimeStateWatchAttempt{ctx: ctx, sandbox: sandbox, pod: pod, credentialID: credentialID, publish: publish, finish: make(chan error, 1)}
	select {
	case w.calls <- attempt:
	case <-ctx.Done():
		return ctx.Err()
	}
	select {
	case err := <-attempt.finish:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func runtimeStateWorkload() (*sandboxapi.Sandbox, *corev1.Pod) {
	return &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}},
		&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "runner", UID: "pod-uid"}, Status: corev1.PodStatus{PodIP: "10.1.2.3"}}
}

func startControlledRuntimeStates(t *testing.T, sandbox *sandboxapi.Sandbox, pod *corev1.Pod) (*RuntimeStates, *controlledRuntimeWatcher, func()) {
	t.Helper()
	watcher := &controlledRuntimeWatcher{calls: make(chan *runtimeStateWatchAttempt, 8)}
	states := NewRuntimeStates(watcher, time.Now)
	// Reconciliation can discover the Pod before the leader-only runnable starts.
	states.Track(sandbox, pod, "credential-id")
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- states.Start(ctx) }()
	return states, watcher, func() {
		cancel()
		if err := <-done; err != nil {
			t.Errorf("RuntimeStates shutdown: %v", err)
		}
	}
}

func runtimeStateResponse(attempt *runtimeStateWatchAttempt, bootID string) *environmentruntimepb.WatchStateResponse {
	return &environmentruntimepb.WatchStateResponse{
		SandboxUid: string(attempt.sandbox.UID), IncarnationUid: string(attempt.pod.UID), EnvironmentRuntimeBootId: bootID,
		Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{
			Bootstrap: environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED,
			IdleSince: timestamppb.New(time.Now().Add(-time.Hour)),
		},
	}
}

func publishRuntimeState(t *testing.T, attempt *runtimeStateWatchAttempt, response *environmentruntimepb.WatchStateResponse) {
	t.Helper()
	if err := attempt.publish(response); err != nil {
		t.Fatalf("publish Runtime state: %v", err)
	}
}

func TestRuntimeStatesInitialSnapshotHeartbeatAndFreshness(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		attempt := <-watcher.calls
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.fresh(time.Now()) || snapshot.BootID != "" {
			t.Fatalf("snapshot before authenticated response = %+v", snapshot)
		}
		if attempt.credentialID != "credential-id" || !states.NeedLeaderElection() {
			t.Fatalf("observation credential = %q, leader-only = %t", attempt.credentialID, states.NeedLeaderElection())
		}
		response := runtimeStateResponse(attempt, "boot-1")
		publishRuntimeState(t, attempt, response)
		first := states.Snapshot(sandbox, string(pod.UID))
		if !first.fresh(time.Now()) || first.Generation == 0 || first.Facts.IdleSince == nil {
			t.Fatalf("first snapshot = %+v", first)
		}
		<-states.Events
		time.Sleep(10 * time.Second)
		publishRuntimeState(t, attempt, response)
		heartbeat := states.Snapshot(sandbox, string(pod.UID))
		if !heartbeat.LastSeen.After(first.LastSeen) || heartbeat.Generation != first.Generation {
			t.Fatalf("heartbeat = %+v, first = %+v", heartbeat, first)
		}
		select {
		case event := <-states.Events:
			t.Fatalf("unchanged heartbeat enqueued reconciliation: %v", event)
		default:
		}
		time.Sleep(runtimeStateTimeout)
		if states.Snapshot(sandbox, string(pod.UID)).fresh(time.Now()) {
			t.Fatal("snapshot remained fresh at heartbeat deadline")
		}
	})
}

func TestRuntimeStatesRejectsInvalidResponseWithoutPublishing(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		attempt := <-watcher.calls
		for _, mutate := range []func(*environmentruntimepb.WatchStateResponse){
			func(r *environmentruntimepb.WatchStateResponse) { r.SandboxUid = "another-sandbox" },
			func(r *environmentruntimepb.WatchStateResponse) { r.IncarnationUid = "old-pod" },
			func(r *environmentruntimepb.WatchStateResponse) { r.EnvironmentRuntimeBootId = "" },
			func(r *environmentruntimepb.WatchStateResponse) { r.ConnectionEpoch = 1 },
			func(r *environmentruntimepb.WatchStateResponse) { r.Facts = nil },
		} {
			response := runtimeStateResponse(attempt, "boot-1")
			mutate(response)
			if err := attempt.publish(response); err == nil {
				t.Fatalf("accepted invalid response: %v", response)
			}
			if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Connected || snapshot.BootID != "" {
				t.Fatalf("invalid response published %+v", snapshot)
			}
		}
	})
}

func TestRuntimeStatesReplacementFencesOldCallbacks(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		first := <-watcher.calls
		publishRuntimeState(t, first, runtimeStateResponse(first, "old-boot"))
		replacement := pod.DeepCopy()
		replacement.UID = "replacement-pod"
		states.Track(sandbox, replacement, "replacement-credential")
		second := <-watcher.calls
		if first.ctx.Err() == nil {
			t.Fatal("replacement did not cancel old observation")
		}
		if err := first.publish(runtimeStateResponse(first, "late-old-boot")); !errors.Is(err, context.Canceled) {
			t.Fatalf("old callback error = %v", err)
		}
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Connected {
			t.Fatalf("old Pod still has state: %+v", snapshot)
		}
		if snapshot := states.Snapshot(sandbox, string(replacement.UID)); snapshot.Connected {
			t.Fatalf("replacement inherited old state: %+v", snapshot)
		}
		publishRuntimeState(t, second, runtimeStateResponse(second, "new-boot"))
		if snapshot := states.Snapshot(sandbox, string(replacement.UID)); snapshot.BootID != "new-boot" || !snapshot.fresh(time.Now()) {
			t.Fatalf("replacement state = %+v", snapshot)
		}
		// A recreated Sandbox with the same namespace/name cannot inherit the old UID's facts.
		newSandbox := sandbox.DeepCopy()
		newSandbox.UID = "recreated-sandbox"
		states.Track(newSandbox, replacement, "replacement-credential")
		third := <-watcher.calls
		if err := second.publish(runtimeStateResponse(second, "late-new-boot")); !errors.Is(err, context.Canceled) {
			t.Fatalf("deleted Sandbox callback error = %v", err)
		}
		publishRuntimeState(t, third, runtimeStateResponse(third, "recreated-boot"))
		if snapshot := states.Snapshot(sandbox, string(replacement.UID)); snapshot.Connected {
			t.Fatalf("deleted Sandbox read replacement facts: %+v", snapshot)
		}
		states.Track(newSandbox, nil, "")
		if snapshot := states.Snapshot(newSandbox, string(replacement.UID)); snapshot.Connected {
			t.Fatalf("removed Pod retained facts: %+v", snapshot)
		}
		if err := third.publish(runtimeStateResponse(third, "late-recreated-boot")); !errors.Is(err, context.Canceled) {
			t.Fatalf("removed Pod callback error = %v", err)
		}
		states.Track(newSandbox, replacement, "replacement-credential")
		fourth := <-watcher.calls
		publishRuntimeState(t, fourth, runtimeStateResponse(fourth, "before-forget"))
		states.Forget(types.NamespacedName{Namespace: newSandbox.Namespace, Name: newSandbox.Name})
		if snapshot := states.Snapshot(newSandbox, string(replacement.UID)); snapshot.Connected {
			t.Fatalf("deleted Sandbox retained facts: %+v", snapshot)
		}
		if err := fourth.publish(runtimeStateResponse(fourth, "after-forget")); !errors.Is(err, context.Canceled) {
			t.Fatalf("forgotten Sandbox callback error = %v", err)
		}
	})
}

func TestRuntimeStatesConnectionLossChangesGenerationAndRejectsOldAttempt(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		first := <-watcher.calls
		response := runtimeStateResponse(first, "same-boot")
		publishRuntimeState(t, first, response)
		before := states.Snapshot(sandbox, string(pod.UID))
		first.finish <- errors.New("transport lost")
		synctest.Wait()
		lost := states.Snapshot(sandbox, string(pod.UID))
		if lost.Connected || lost.fresh(time.Now()) {
			t.Fatalf("disconnected watch remained usable: %+v", lost)
		}
		if err := first.publish(runtimeStateResponse(first, "late-boot")); !errors.Is(err, context.Canceled) {
			t.Fatalf("completed watch callback error = %v", err)
		}
		time.Sleep(time.Second)
		second := <-watcher.calls
		publishRuntimeState(t, second, response)
		after := states.Snapshot(sandbox, string(pod.UID))
		if !after.fresh(time.Now()) || after.Generation <= before.Generation || !after.Facts.IdleSince.Equal(*before.Facts.IdleSince) {
			t.Fatalf("reconnected snapshot = %+v, before = %+v", after, before)
		}
		if err := first.publish(runtimeStateResponse(first, "very-late-boot")); !errors.Is(err, context.Canceled) {
			t.Fatalf("superseded attempt callback error = %v", err)
		}
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.BootID != after.BootID || snapshot.Generation != after.Generation {
			t.Fatalf("late callback changed current observation: %+v", snapshot)
		}
	})
}

func TestRuntimeStatesBootstrapFailureIsTerminalUntilPodReplacement(t *testing.T) {
	for _, terminal := range []environmentruntimestatepb.EnvironmentRuntimeFacts_Bootstrap{
		environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED,
		environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED,
	} {
		t.Run(terminal.String(), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				sandbox, pod := runtimeStateWorkload()
				states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
				defer stop()
				first := <-watcher.calls
				response := runtimeStateResponse(first, "boot-1")
				response.Facts.Bootstrap = terminal
				publishRuntimeState(t, first, response)
				first.finish <- errors.New("Runtime restarted")
				time.Sleep(time.Second)
				second := <-watcher.calls
				publishRuntimeState(t, second, runtimeStateResponse(second, "boot-2"))
				want := BootstrapFailed
				if terminal == environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED {
					want = BootstrapInterrupted
				}
				if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Facts.Bootstrap != want || snapshot.BootID != "boot-2" {
					t.Fatalf("Runtime restart cleared terminal bootstrap: %+v", snapshot)
				}
				replacement := pod.DeepCopy()
				replacement.UID = "replacement-pod"
				states.Track(sandbox, replacement, "new-credential")
				third := <-watcher.calls
				publishRuntimeState(t, third, runtimeStateResponse(third, "boot-3"))
				if snapshot := states.Snapshot(sandbox, string(replacement.UID)); snapshot.Facts.Bootstrap != BootstrapSucceeded {
					t.Fatalf("replacement inherited terminal bootstrap: %+v", snapshot)
				}
			})
		})
	}
}

func TestRuntimeStatesLeaderTakeoverStartsWithoutFacts(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		oldStates, oldWatcher, stopOld := startControlledRuntimeStates(t, sandbox, pod)
		first := <-oldWatcher.calls
		publishRuntimeState(t, first, runtimeStateResponse(first, "old-leader-boot"))
		stopOld()
		if snapshot := oldStates.Snapshot(sandbox, string(pod.UID)); snapshot.Connected {
			t.Fatalf("stopped leader retained facts: %+v", snapshot)
		}
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		second := <-watcher.calls
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Connected || snapshot.Facts.IdleSince != nil {
			t.Fatalf("new leader inherited idle evidence: %+v", snapshot)
		}
		if err := first.publish(runtimeStateResponse(first, "old-leader-late")); !errors.Is(err, context.Canceled) {
			t.Fatalf("old leader callback error = %v", err)
		}
		publishRuntimeState(t, second, runtimeStateResponse(second, "new-leader-boot"))
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); !snapshot.fresh(time.Now()) {
			t.Fatalf("new leader failed to establish fresh state: %+v", snapshot)
		}
	})
}

func TestRuntimeStatesAddressChangePreservesTerminalBootstrapButResetsConnection(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		first := <-watcher.calls
		response := runtimeStateResponse(first, "boot-1")
		response.Facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED
		publishRuntimeState(t, first, response)
		before := states.Snapshot(sandbox, string(pod.UID))
		readdressed := pod.DeepCopy()
		readdressed.Status.PodIP = "10.1.2.4"
		states.Track(sandbox, readdressed, "credential-id")
		second := <-watcher.calls
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Connected || snapshot.Facts.IdleSince != nil {
			t.Fatalf("address change retained connection/idle evidence: %+v", snapshot)
		}
		publishRuntimeState(t, second, runtimeStateResponse(second, "boot-2"))
		if snapshot := states.Snapshot(sandbox, string(pod.UID)); snapshot.Facts.Bootstrap != BootstrapFailed || snapshot.Generation <= before.Generation {
			t.Fatalf("address change cleared terminal bootstrap or reused attempt: %+v", snapshot)
		}
	})
}

func TestRuntimeStatesLeaderTakeoverPreservesPersistedBootstrapFailure(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		sandbox, pod := runtimeStateWorkload()
		sandbox.Spec.Bootstrap = "exit 1"
		sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: pod.Name, UID: string(pod.UID)}
		sandbox.Status.Conditions = []metav1.Condition{{Type: sandboxapi.ConditionBootstrapped, Status: metav1.ConditionFalse, Reason: "BootstrapFailed", Message: "persisted terminal failure"}}
		states, watcher, stop := startControlledRuntimeStates(t, sandbox, pod)
		defer stop()
		first := <-watcher.calls
		publishRuntimeState(t, first, runtimeStateResponse(first, "new-leader-boot"))
		if condition := runtimeBootstrappedCondition(sandbox, pod, states.Snapshot(sandbox, string(pod.UID))); condition.Status != metav1.ConditionFalse || condition.Reason != "BootstrapFailed" {
			t.Fatalf("leader takeover cleared persisted terminal bootstrap: %+v", condition)
		}
		replacement := pod.DeepCopy()
		replacement.UID = "replacement-pod"
		states.Track(sandbox, replacement, "replacement-credential")
		second := <-watcher.calls
		publishRuntimeState(t, second, runtimeStateResponse(second, "replacement-boot"))
		if condition := runtimeBootstrappedCondition(sandbox, replacement, states.Snapshot(sandbox, string(replacement.UID))); condition.Status != metav1.ConditionTrue {
			t.Fatalf("replacement inherited persisted terminal bootstrap: %+v", condition)
		}
	})
}
