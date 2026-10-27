package sandboxcontroller

import (
	"context"
	"errors"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const idleSuspendAfter = 10 * time.Minute

// The runner's clock is an hour behind the controller's, which the idle timer must not read.
var runnerIdleSince = testNow.Add(-time.Hour)

// publishIdleSince is the Runner Gateway recording on the Lease that the runner went idle at at, or, for nil,
// that it is busy.
func publishIdleSince(t *testing.T, c client.Client, at *time.Time) {
	t.Helper()
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + sandboxlease.RunnerLeaseSuffix}, &lease); err != nil {
		t.Fatal(err)
	}
	if at == nil {
		delete(lease.Annotations, sandboxlease.IdleSinceAnnotation)
	} else {
		lease.Annotations[sandboxlease.IdleSinceAnnotation] = at.UTC().Format(time.RFC3339Nano)
	}
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
}

func setIdleSuspendAfter(t *testing.T, c client.Client, after *time.Duration) {
	t.Helper()
	sandbox := getSandbox(t, c)
	sandbox.Spec.IdleSuspendAfter = nil
	if after != nil {
		sandbox.Spec.IdleSuspendAfter = &metav1.Duration{Duration: *after}
	}
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
}

// idleRunner is a running Sandbox whose runner has gone idle, with a clock the test advances through now.
func idleRunner(t *testing.T, after *time.Duration) (r *Reconciler, c client.Client, now *time.Time) {
	t.Helper()
	r, c = runningWithRunner(t)
	now = new(time.Time)
	*now = testNow
	r.Now = func() time.Time { return *now }
	// A connection that outlasts the test, so its lapse schedules no recheck of its own.
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + sandboxlease.RunnerLeaseSuffix}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Spec.LeaseDurationSeconds = new(int32)
	*lease.Spec.LeaseDurationSeconds = 7 * 24 * 3600
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	setIdleSuspendAfter(t, c, after)
	publishIdleSince(t, c, &runnerIdleSince)
	return r, c, now
}

func reconcileResult(t *testing.T, r *Reconciler) ctrl.Result {
	t.Helper()
	result, err := r.Reconcile(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func durationPtr(d time.Duration) *time.Duration { return &d }

type countingStatusClient struct {
	client.Client
	statusUpdates int
}

func (c *countingStatusClient) Status() client.SubResourceWriter {
	return &countingStatusWriter{SubResourceWriter: c.Client.Status(), statusUpdates: &c.statusUpdates}
}

type countingStatusWriter struct {
	client.SubResourceWriter
	statusUpdates *int
}

func (w *countingStatusWriter) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	*w.statusUpdates = *w.statusUpdates + 1
	return w.SubResourceWriter.Update(ctx, obj, opts...)
}

func TestARunnerIdleForIdleSuspendAfterSuspendsItsSandbox(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Quiescing: true}

	if result := reconcileResult(t, r); quiescer.calls != 0 || result.RequeueAfter != idleSuspendAfter {
		t.Fatalf("first sight of the idle runner: quiesce calls = %d, requeue = %v; want 0 and the full wait", quiescer.calls, result.RequeueAfter)
	}
	if got, want := getSandbox(t, c).Status.IdleSuspendAt, metav1.NewTime(testNow.Add(idleSuspendAfter)); got == nil || !got.Equal(&want) {
		t.Fatalf("idleSuspendAt = %v, want %v", got, want)
	}
	*now = now.Add(idleSuspendAfter - time.Minute)
	if result := reconcileResult(t, r); quiescer.calls != 0 || result.RequeueAfter != time.Minute {
		t.Fatalf("a minute early: quiesce calls = %d, requeue = %v; want 0 and the minute left", quiescer.calls, result.RequeueAfter)
	}

	*now = now.Add(time.Minute)
	reconcileOnce(t, r)
	if quiescer.calls != 1 || quiescer.ifIdleSince == nil || !quiescer.ifIdleSince.Equal(runnerIdleSince) {
		t.Fatalf("quiesce calls = %d, ifIdleSince = %v; want one request carrying the runner's idle-since", quiescer.calls, quiescer.ifIdleSince)
	}
	suspended := getSandbox(t, c)
	if suspended.Status.SuspendedBy == nil || *suspended.Status.SuspendedBy != sandboxapi.SuspendedByIdle ||
		suspended.Status.SuspendedAtWakeGeneration == nil || *suspended.Status.SuspendedAtWakeGeneration != suspended.Spec.WakeGeneration {
		t.Fatalf("suspendedBy = %v at generation %v, want Idle at generation %d", suspended.Status.SuspendedBy, suspended.Status.SuspendedAtWakeGeneration, suspended.Spec.WakeGeneration)
	}
	if suspended.Status.OperatingMode != sandboxapi.OperatingModeSuspended || runningReason(t, c) != "Quiescing" || getPod(t, c) == nil {
		t.Fatalf("mode = %s, Running reason = %s, Pod present = %v; want Suspended while the Pod waits for the seal", suspended.Status.OperatingMode, runningReason(t, c), getPod(t, c) != nil)
	}
	if suspended.Status.IdleSuspendAt != nil {
		t.Fatalf("idleSuspendAt = %v after suspension began, want absent", suspended.Status.IdleSuspendAt)
	}

	quiescer.result = QuiesceResult{}
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("the idle Sandbox kept its Pod after its runner sealed")
	}
	if getPVC(t, c) == nil {
		t.Fatal("suspending an idle Sandbox deleted its state PVC")
	}
}

func TestIdleSuspensionIsOffUnlessTheSandboxAsksForIt(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	reconcileOnce(t, r)
	if getSandbox(t, c).Status.IdleSuspendAt == nil {
		t.Fatal("an enabled idle timer did not publish its deadline")
	}
	setIdleSuspendAfter(t, c, nil)
	reconcileOnce(t, r)
	*now = now.Add(24 * time.Hour)
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 0 || getSandbox(t, c).Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("an idle Sandbox with no idleSuspendAfter was asked to quiesce %d times", calls)
	}
	if got := getSandbox(t, c).Status.IdleSuspendAt; got != nil {
		t.Fatalf("idleSuspendAt = %v after suspension was disabled, want absent", got)
	}
}

func TestIdleSuspendAtDoesNotCauseStatusWritesAsTheCountdownAdvances(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	statusClient := &countingStatusClient{Client: r.Client}
	r.Client = statusClient
	*now = now.Add(123 * time.Millisecond)

	reconcileOnce(t, r)
	first := getSandbox(t, c)
	wantDeadline := metav1.NewTime(now.Add(idleSuspendAfter).Truncate(time.Second))
	if first.Status.IdleSuspendAt == nil || !first.Status.IdleSuspendAt.Equal(&wantDeadline) || first.Status.IdleSuspendAt.Nanosecond() != 0 {
		t.Fatalf("published idleSuspendAt = %v, want whole-second %v", first.Status.IdleSuspendAt, wantDeadline)
	}
	writesAfterPublication, resourceVersion := statusClient.statusUpdates, first.ResourceVersion
	if writesAfterPublication == 0 {
		t.Fatal("publishing idleSuspendAt did not write status")
	}

	*now = now.Add(time.Minute)
	if result := reconcileResult(t, r); result.RequeueAfter != idleSuspendAfter-time.Minute {
		t.Fatalf("countdown requeue = %v, want %v", result.RequeueAfter, idleSuspendAfter-time.Minute)
	}
	second := getSandbox(t, c)
	if second.Status.IdleSuspendAt == nil || !second.Status.IdleSuspendAt.Equal(&wantDeadline) {
		t.Fatalf("idleSuspendAt changed during countdown: got %v", second.Status.IdleSuspendAt)
	}
	if statusClient.statusUpdates != writesAfterPublication || second.ResourceVersion != resourceVersion {
		t.Fatalf("countdown caused status writes or resourceVersion churn: updates %d -> %d, resourceVersion %q -> %q", writesAfterPublication, statusClient.statusUpdates, resourceVersion, second.ResourceVersion)
	}
}

func TestIdleSuspendDeadlineRestartsWithControllerMemoryTimer(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	*now = now.Add(123 * time.Millisecond)
	reconcileOnce(t, r)
	firstDeadline := getSandbox(t, c).Status.IdleSuspendAt
	wantFirstDeadline := metav1.NewTime(now.Add(idleSuspendAfter).Truncate(time.Second))
	if firstDeadline == nil || !firstDeadline.Equal(&wantFirstDeadline) {
		t.Fatalf("initial idleSuspendAt = %v, want %v", firstDeadline, wantFirstDeadline)
	}

	*now = now.Add(3 * time.Minute)
	restarted := &Reconciler{
		Client: r.Client, Objects: r.Objects, KubeVirt: r.KubeVirt,
		Quiescer: r.Quiescer, Leases: r.Leases, Now: r.Now,
	}
	reconcileOnce(t, restarted)
	got := getSandbox(t, c).Status.IdleSuspendAt
	wantRestartDeadline := metav1.NewTime(now.Add(idleSuspendAfter).Truncate(time.Second))
	if got == nil || !got.Equal(&wantRestartDeadline) {
		t.Fatalf("idleSuspendAt after controller restart = %v, want %v", got, wantRestartDeadline)
	}
}

func TestIdleSuspendAtClearsWhenConvergenceIsBlocked(t *testing.T) {
	r, c, _ := idleRunner(t, durationPtr(idleSuspendAfter))
	reconcileOnce(t, r)
	sandbox := getSandbox(t, c)
	if sandbox.Status.IdleSuspendAt == nil {
		t.Fatal("the running idle Sandbox has no published deadline")
	}

	deadline := sandbox.Status.IdleSuspendAt.DeepCopy()
	if _, err := r.updateStatus(context.Background(), sandbox, workload{pod: getPod(t, c)}, &unready{reason: "Blocked", message: "test blocker"}, deadline); err != nil {
		t.Fatal(err)
	}
	if got := getSandbox(t, c).Status.IdleSuspendAt; got != nil {
		t.Fatalf("idleSuspendAt = %v while blocked, want absent", got)
	}
}

func TestSettingIdleSuspendAfterOnAnAlreadyIdleSandboxCountsFromWhenTheControllerFirstSawItIdle(t *testing.T) {
	r, c, now := idleRunner(t, nil)
	reconcileOnce(t, r)
	*now = now.Add(time.Hour)
	setIdleSuspendAfter(t, c, durationPtr(idleSuspendAfter))
	reconcileOnce(t, r)
	if calls := r.Quiescer.(*fakeQuiescer).calls; calls != 1 {
		t.Fatalf("quiesce calls = %d, want 1: the runner had been idle longer than the new setting", calls)
	}
}

func TestARunnerThatWorksAgainRestartsTheIdleTimer(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	quiescer := r.Quiescer.(*fakeQuiescer)
	reconcileOnce(t, r)

	*now = now.Add(idleSuspendAfter - time.Minute)
	publishIdleSince(t, c, nil)
	reconcileOnce(t, r)
	if got := getSandbox(t, c).Status.IdleSuspendAt; got != nil {
		t.Fatalf("idleSuspendAt = %v while runner is busy, want absent", got)
	}
	workDone := now.Add(-time.Second)
	publishIdleSince(t, c, &workDone)
	reconcileOnce(t, r)
	if got, want := getSandbox(t, c).Status.IdleSuspendAt, metav1.NewTime(now.Add(idleSuspendAfter).Truncate(time.Second)); got == nil || !got.Equal(&want) {
		t.Fatalf("idleSuspendAt = %v after runner became idle again, want %v", got, want)
	}

	*now = now.Add(idleSuspendAfter - time.Second)
	reconcileOnce(t, r)
	if quiescer.calls != 0 {
		t.Fatal("the timer ran on from before the runner worked")
	}
	*now = now.Add(time.Second)
	reconcileOnce(t, r)
	if quiescer.calls != 1 || quiescer.ifIdleSince == nil || !quiescer.ifIdleSince.Equal(workDone) {
		t.Fatalf("quiesce calls = %d, ifIdleSince = %v; want one request for the new idle period", quiescer.calls, quiescer.ifIdleSince)
	}
}

func TestARunnerThatRefusesToQuiesceKeepsItsSandboxRunningAndTheTimerStartsOver(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.err = status.Error(codes.Aborted, "the Sandbox is busy")
	reconcileOnce(t, r)
	*now = now.Add(idleSuspendAfter)

	result := reconcileResult(t, r)
	if quiescer.calls != 1 || result.RequeueAfter != idleSuspendAfter {
		t.Fatalf("quiesce calls = %d, requeue = %v; want one refused request and a full wait", quiescer.calls, result.RequeueAfter)
	}
	if got := getSandbox(t, c); got.Status.OperatingMode != sandboxapi.OperatingModeRunning || got.Status.SuspendedBy != nil {
		t.Fatalf("mode = %s, suspendedBy = %v after a refusal, want Running and none", got.Status.OperatingMode, got.Status.SuspendedBy)
	}
	if got, want := getSandbox(t, c).Status.IdleSuspendAt, metav1.NewTime(now.Add(idleSuspendAfter).Truncate(time.Second)); got == nil || !got.Equal(&want) {
		t.Fatalf("idleSuspendAt after a busy refusal = %v, want %v", got, want)
	}
	reconcileOnce(t, r)
	if quiescer.calls != 1 {
		t.Fatal("the refused request was repeated before the timer ran again")
	}
	*now = now.Add(idleSuspendAfter)
	reconcileOnce(t, r)
	if quiescer.calls != 2 {
		t.Fatalf("quiesce calls = %d, want 2 once the timer ran again", quiescer.calls)
	}
}

func TestAnUnreachableIdleRunnerIsAskedAgainAtTheQuiesceDeadline(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.err = errors.New("runner unreachable")
	reconcileOnce(t, r)
	*now = now.Add(idleSuspendAfter)

	if result := reconcileResult(t, r); quiescer.calls != 1 || result.RequeueAfter != quiesceDeadline {
		t.Fatalf("quiesce calls = %d, requeue = %v; want one failed request and a retry at the deadline", quiescer.calls, result.RequeueAfter)
	}
	if got, want := getSandbox(t, c).Status.IdleSuspendAt, metav1.NewTime(now.Add(quiesceDeadline).Truncate(time.Second)); got == nil || !got.Equal(&want) {
		t.Fatalf("idleSuspendAt after an unreachable runner = %v, want %v", got, want)
	}
	if getSandbox(t, c).Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatal("a failed request suspended the Sandbox")
	}
	reconcileOnce(t, r)
	if quiescer.calls != 1 {
		t.Fatal("watch events bypassed the retry deadline")
	}
	*now = now.Add(quiesceDeadline)
	quiescer.err = nil
	quiescer.result = QuiesceResult{Quiescing: true}
	reconcileOnce(t, r)
	if quiescer.calls != 2 || getSandbox(t, c).Status.OperatingMode != sandboxapi.OperatingModeSuspended {
		t.Fatalf("quiesce calls = %d, mode = %s; want a second request that suspended the Sandbox", quiescer.calls, getSandbox(t, c).Status.OperatingMode)
	}
	if got := getSandbox(t, c).Status.IdleSuspendAt; got != nil {
		t.Fatalf("idleSuspendAt = %v after the runner accepted suspension, want absent", got)
	}
}

// A wake that lands while the runner enters lameduck leaves a runner that refuses every Session, so it is sealed
// and replaced rather than recorded as an idle suspension the wake would never end.
func TestAWakeDuringTheIdleQuiesceReplacesTheIncarnation(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	quiescer := r.Quiescer.(*fakeQuiescer)
	quiescer.result = QuiesceResult{Quiescing: true}
	quiescer.before = func() {
		quiescer.before = nil
		woken := getSandbox(t, c)
		woken.Spec.WakeGeneration++
		if err := c.Update(context.Background(), woken); err != nil {
			t.Fatal(err)
		}
	}
	reconcileOnce(t, r)
	*now = now.Add(idleSuspendAfter)

	if _, err := r.Reconcile(context.Background(), request); err == nil {
		t.Fatal("the suspension was written over a wake")
	}
	if got := getSandbox(t, c); got.Status.SuspendedBy != nil || got.Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("suspendedBy = %v, mode = %s; want no suspension recorded", got.Status.SuspendedBy, got.Status.OperatingMode)
	}

	publishQuiesceStage(t, c, "draining")
	reconcileOnce(t, r)
	if runningReason(t, c) != "Quiescing" || getPod(t, c) == nil {
		t.Fatalf("a runner in lameduck was not sealed before it was replaced: Running reason = %s", runningReason(t, c))
	}
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if pod := getPod(t, c); pod != nil && string(pod.UID) == "uid-pod" {
		t.Fatal("the woken Sandbox kept the incarnation whose runner quiesced")
	}
}

func TestSuspendedBySaysWhoStoppedTheSandbox(t *testing.T) {
	r, c, now := idleRunner(t, durationPtr(idleSuspendAfter))
	if by := getSandbox(t, c).Status.SuspendedBy; by != nil {
		t.Fatalf("a running Sandbox is suspended by %s", *by)
	}
	reconcileOnce(t, r)
	*now = now.Add(idleSuspendAfter)
	reconcileOnce(t, r)
	if by := getSandbox(t, c).Status.SuspendedBy; by == nil || *by != sandboxapi.SuspendedByIdle {
		t.Fatalf("suspendedBy = %v, want Idle", by)
	}

	// A requester that stops the Sandbox itself takes over the reason.
	setRunning(t, c, false)
	reconcileOnce(t, r)
	stopped := getSandbox(t, c)
	if stopped.Status.SuspendedBy == nil || *stopped.Status.SuspendedBy != sandboxapi.SuspendedByRequested || stopped.Status.SuspendedAtWakeGeneration != nil {
		t.Fatalf("suspendedBy = %v at generation %v, want Requested and no generation", stopped.Status.SuspendedBy, stopped.Status.SuspendedAtWakeGeneration)
	}

	resumed := getSandbox(t, c)
	resumed.Spec.Running, resumed.Spec.WakeGeneration = true, resumed.Spec.WakeGeneration+1
	if err := c.Update(context.Background(), resumed); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if got := getSandbox(t, c); got.Status.SuspendedBy != nil || got.Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("suspendedBy = %v, mode = %s after the resume, want none and Running", got.Status.SuspendedBy, got.Status.OperatingMode)
	}
}
