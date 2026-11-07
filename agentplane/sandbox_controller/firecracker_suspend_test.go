package sandboxcontroller

import (
	"context"
	"errors"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	runtimeschema "k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type suspensionPrepareCall struct {
	generation int64
	idleSince  time.Time
}

type testRuntimeSuspender struct {
	prepare func(context.Context, *sandboxapi.Sandbox, int64, time.Time) error
	resume  func(context.Context, *sandboxapi.Sandbox, int64) error

	prepares []suspensionPrepareCall
	resumes  []int64
}

func (s *testRuntimeSuspender) Prepare(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64, idleSince time.Time) error {
	s.prepares = append(s.prepares, suspensionPrepareCall{generation: generation, idleSince: idleSince})
	if s.prepare != nil {
		return s.prepare(ctx, sandbox, generation, idleSince)
	}
	return nil
}

func (s *testRuntimeSuspender) Resume(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64) error {
	s.resumes = append(s.resumes, generation)
	if s.resume != nil {
		return s.resume(ctx, sandbox, generation)
	}
	return nil
}

func firecrackerIdleController(t *testing.T) (*Reconciler, client.Client, *time.Time, *testFirecrackerStatus, *testRuntimeSuspender) {
	t.Helper()
	r, c := newReconciler(t, true, firecrackerTemplate())
	now := testNow
	r.Now = func() time.Time { return now }
	status := &testFirecrackerStatus{state: FirecrackerState{PodUID: "uid-pod", Phase: "running"}}
	suspender := &testRuntimeSuspender{}
	r.FirecrackerStatus = status
	r.Suspender = suspender
	for attempt := 0; attempt < 8 && getPod(t, c) == nil; attempt++ {
		if _, err := r.Reconcile(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	pod := getPod(t, c)
	if pod == nil || !isFirecrackerPod(pod) {
		t.Fatal("Firecracker launcher was not created")
	}
	status.state.PodUID = string(pod.UID)
	connect(t, c, string(pod.UID))
	var lease coordinationv1.Lease
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + sandboxlease.EnvironmentRuntimeLeaseSuffix}, &lease); err != nil {
		t.Fatal(err)
	}
	lease.Spec.LeaseDurationSeconds = new(int32)
	*lease.Spec.LeaseDurationSeconds = 7 * 24 * 3600
	if err := c.Update(context.Background(), &lease); err != nil {
		t.Fatal(err)
	}
	setIdleSuspendAfter(t, c, durationPtr(idleSuspendAfter))
	publishIdleSince(t, c, &runnerIdleSince)
	reconcileOnce(t, r) // The controller first observes this idle period at its own clock.
	return r, c, &now, status, suspender
}

func firecrackerToResumingAfterWake(t *testing.T, r *Reconciler, c client.Client, now *time.Time, status *testFirecrackerStatus) {
	t.Helper()
	*now = now.Add(idleSuspendAfter)
	reconcileResult(t, r) // Persist Preparing, call Prepare, and move to Checkpointing.
	reconcileResult(t, r) // Project the checkpoint intent to the launcher.
	status.state.Phase = "suspended"
	reconcileResult(t, r) // With no wake yet, publish the temporary Stopped stage.
	if got := getSandbox(t, c); got.Status.StopStage != sandboxapi.StopStageStopped {
		t.Fatalf("checkpoint StopStage = %q, want Stopped", got.Status.StopStage)
	}
	sandbox := getSandbox(t, c)
	sandbox.Spec.WakeGeneration++
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	reconcileResult(t, r) // Finish checkpointing and clear the temporary stop stage.
}

func TestFirecrackerLateBusyLeaseRefusesIdleSuspendWithoutCheckpointIntent(t *testing.T) {
	r, c, now, _, suspender := firecrackerIdleController(t)
	suspender.prepare = func(_ context.Context, _ *sandboxapi.Sandbox, _ int64, _ time.Time) error {
		stored := getSandbox(t, c)
		if attempt := stored.Status.FirecrackerSuspension; attempt == nil || attempt.Phase != sandboxapi.FirecrackerSuspendPreparing || stored.Status.OperatingMode != sandboxapi.OperatingModeSuspended {
			t.Fatalf("Prepare ran before its admission fence was persisted: attempt=%+v mode=%s", stored.Status.FirecrackerSuspension, stored.Status.OperatingMode)
		}
		publishIdleSince(t, c, nil) // The Runtime sees a new busy fact after the controller's idle read.
		return grpcstatus.Error(codes.Aborted, "runner became busy")
	}

	*now = now.Add(idleSuspendAfter)
	reconcileResult(t, r)
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension == nil || got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendResuming || got.Status.SuspendedBy != nil {
		t.Fatalf("busy refusal did not clear the idle reason and enter Resuming: %+v", got.Status)
	}
	if got := getPod(t, c); got.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("busy refusal requested a checkpoint: intent=%q", got.Annotations[FirecrackerRunningAnnotation])
	}
	reconcileResult(t, r)
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendReleased || got.Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("busy refusal did not release the Sandbox: phase=%s mode=%s", got.Status.FirecrackerSuspension.Phase, got.Status.OperatingMode)
	}
	if len(suspender.resumes) != 1 {
		t.Fatalf("Resume calls = %d, want one after refusal", len(suspender.resumes))
	}
}

func TestFirecrackerLostPrepareReplyRetriesPersistedGenerationAfterControllerRestart(t *testing.T) {
	r, c, now, _, suspender := firecrackerIdleController(t)
	suspender.prepare = func(_ context.Context, _ *sandboxapi.Sandbox, _ int64, _ time.Time) error {
		stored := getSandbox(t, c)
		if attempt := stored.Status.FirecrackerSuspension; attempt == nil || attempt.Phase != sandboxapi.FirecrackerSuspendPreparing || stored.Status.OperatingMode != sandboxapi.OperatingModeSuspended {
			t.Fatalf("Prepare ran before its admission fence was persisted: attempt=%+v mode=%s", stored.Status.FirecrackerSuspension, stored.Status.OperatingMode)
		}
		if len(suspender.prepares) == 1 {
			return errors.New("reply lost after Runtime may have applied Prepare")
		}
		return nil
	}
	*now = now.Add(idleSuspendAfter)
	reconcileResult(t, r)
	if got := getSandbox(t, c).Status.FirecrackerSuspension; got.Phase != sandboxapi.FirecrackerSuspendPreparing {
		t.Fatalf("lost Prepare reply advanced phase to %s", got.Phase)
	}

	restarted := &Reconciler{
		Client: r.Client, Objects: r.Objects, KubeVirt: r.KubeVirt,
		Quiescer: r.Quiescer, Leases: r.Leases, RunnerCertificateIssuer: r.RunnerCertificateIssuer,
		Now: r.Now, FirecrackerStatus: r.FirecrackerStatus, Suspender: suspender,
	}
	reconcileResult(t, restarted)
	if len(suspender.prepares) != 2 || suspender.prepares[0].generation != 1 || suspender.prepares[1].generation != 1 ||
		!suspender.prepares[0].idleSince.Equal(runnerIdleSince) || !suspender.prepares[1].idleSince.Equal(runnerIdleSince) {
		t.Fatalf("Prepare retries = %+v, want both calls to reuse generation 1 and idle-since %s", suspender.prepares, runnerIdleSince)
	}
	if got := getSandbox(t, c).Status.FirecrackerSuspension; got.Generation != 1 || got.Phase != sandboxapi.FirecrackerSuspendCheckpointing {
		t.Fatalf("retried Prepare phase = %+v, want generation 1 Checkpointing", got)
	}
}

func TestFirecrackerConcurrentWakeWaitsForCheckpointBeforeResumeAndClearsStopStage(t *testing.T) {
	r, c, now, status, suspender := firecrackerIdleController(t)
	*now = now.Add(idleSuspendAfter)
	reconcileResult(t, r) // Prepare is acknowledged; the launcher still reports running.
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendCheckpointing {
		t.Fatalf("after Prepare, phase = %s, want Checkpointing", got.Status.FirecrackerSuspension.Phase)
	}
	if got := getPod(t, c); got.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("launcher intent changed before the concurrent wake: %q", got.Annotations[FirecrackerRunningAnnotation])
	}
	sandbox := getSandbox(t, c)
	sandbox.Spec.WakeGeneration++ // Wake before the checkpoint intent is projected.
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	reconcileResult(t, r)
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendCheckpointing ||
		got.Status.OperatingMode != sandboxapi.OperatingModeSuspended {
		t.Fatalf("concurrent wake abandoned the checkpoint fence: phase=%s mode=%s", got.Status.FirecrackerSuspension.Phase, got.Status.OperatingMode)
	}
	if got := getPod(t, c); got.Annotations[FirecrackerRunningAnnotation] != "false" {
		t.Fatalf("concurrent wake skipped the required checkpoint intent: %q", got.Annotations[FirecrackerRunningAnnotation])
	}
	if len(suspender.resumes) != 0 {
		t.Fatal("Runtime admission resumed before the checkpoint began")
	}
	status.state.Phase = "suspended"
	reconcileResult(t, r) // Only now may a concurrent wake move the attempt to Resuming.
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendResuming ||
		got.Status.StopStage != "" || got.Status.OperatingMode != sandboxapi.OperatingModeSuspended {
		t.Fatalf("checkpointed wake did not enter fenced Resuming: phase=%s stopStage=%q mode=%s", got.Status.FirecrackerSuspension.Phase, got.Status.StopStage, got.Status.OperatingMode)
	}
	if len(suspender.resumes) != 0 {
		t.Fatal("Runtime admission resumed before the launcher had resumed")
	}
	reconcileResult(t, r) // Ask the launcher to resume; it still reports suspended.
	if got := getPod(t, c); got.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("launcher resume intent = %q, want true", got.Annotations[FirecrackerRunningAnnotation])
	}
	if len(suspender.resumes) != 0 {
		t.Fatal("Runtime admission resumed before the launcher reported running")
	}
	status.state.Phase = "running"
	reconcileResult(t, r)
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendReleased ||
		got.Status.OperatingMode != sandboxapi.OperatingModeRunning || got.Status.StopStage != "" {
		t.Fatalf("resumed Sandbox was not released cleanly: phase=%s mode=%s stopStage=%q", got.Status.FirecrackerSuspension.Phase, got.Status.OperatingMode, got.Status.StopStage)
	}
	if len(suspender.resumes) != 1 || suspender.resumes[0] != 1 {
		t.Fatalf("Resume calls = %v, want one call for generation 1", suspender.resumes)
	}
}

func TestFirecrackerResumeLostReplyRetainsFenceAndRetriesSameGeneration(t *testing.T) {
	r, c, now, status, suspender := firecrackerIdleController(t)
	firecrackerToResumingAfterWake(t, r, c, now, status)
	reconcileResult(t, r) // Set launcher running intent while it still reports suspended.
	status.state.Phase = "running"
	suspender.resume = func(_ context.Context, sandbox *sandboxapi.Sandbox, generation int64) error {
		stored := getSandbox(t, c)
		if attempt := stored.Status.FirecrackerSuspension; attempt == nil || attempt.Phase != sandboxapi.FirecrackerSuspendResuming ||
			stored.Status.OperatingMode != sandboxapi.OperatingModeSuspended || generation != 1 {
			t.Fatalf("Resume ran without the persisted generation fence: attempt=%+v mode=%s generation=%d", stored.Status.FirecrackerSuspension, stored.Status.OperatingMode, generation)
		}
		if len(suspender.resumes) == 1 {
			return errors.New("reply lost after Runtime may have reopened admission")
		}
		return nil
	}
	reconcileResult(t, r)
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendResuming ||
		got.Status.OperatingMode != sandboxapi.OperatingModeSuspended || got.Status.StopStage != "" {
		t.Fatalf("lost Resume reply dropped the fence: phase=%s mode=%s stopStage=%q", got.Status.FirecrackerSuspension.Phase, got.Status.OperatingMode, got.Status.StopStage)
	}
	reconcileResult(t, r)
	if len(suspender.resumes) != 2 || suspender.resumes[0] != 1 || suspender.resumes[1] != 1 {
		t.Fatalf("Resume retries = %v, want both calls for generation 1", suspender.resumes)
	}
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendReleased || got.Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("successful Resume did not release the Sandbox: phase=%s mode=%s", got.Status.FirecrackerSuspension.Phase, got.Status.OperatingMode)
	}
}

type firecrackerStatusConflictClient struct{ client.Client }

func (c firecrackerStatusConflictClient) Status() client.SubResourceWriter {
	return firecrackerStatusConflictWriter{SubResourceWriter: c.Client.Status()}
}

type firecrackerStatusConflictWriter struct{ client.SubResourceWriter }

func (w firecrackerStatusConflictWriter) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	if sandbox, ok := obj.(*sandboxapi.Sandbox); ok && sandbox.Status.FirecrackerSuspension != nil &&
		sandbox.Status.FirecrackerSuspension.Phase == sandboxapi.FirecrackerSuspendPreparing {
		return apierrors.NewConflict(runtimeschema.GroupResource{Resource: "sandboxes"}, sandbox.Name, errors.New("injected status write conflict"))
	}
	return w.SubResourceWriter.Update(ctx, obj, opts...)
}

func TestFirecrackerPrepareIsNotCalledWhenPersistingFenceConflicts(t *testing.T) {
	r, c, now, _, suspender := firecrackerIdleController(t)
	r.Client = firecrackerStatusConflictClient{Client: r.Client}
	*now = now.Add(idleSuspendAfter)
	_, err := r.Reconcile(context.Background(), request)
	if !apierrors.IsConflict(err) {
		t.Fatalf("Reconcile() error = %v, want status conflict", err)
	}
	if len(suspender.prepares) != 0 {
		t.Fatalf("Prepare calls = %d after status conflict, want 0", len(suspender.prepares))
	}
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension != nil || got.Status.OperatingMode != sandboxapi.OperatingModeRunning {
		t.Fatalf("failed fence write changed stored status: attempt=%+v mode=%s", got.Status.FirecrackerSuspension, got.Status.OperatingMode)
	}
	if got := getPod(t, c); got.Annotations[FirecrackerRunningAnnotation] != "true" {
		t.Fatalf("failed fence write changed launcher intent to %q", got.Annotations[FirecrackerRunningAnnotation])
	}
}

func TestFirecrackerReleasedSuspensionIncrementsGenerationForNextAttempt(t *testing.T) {
	r, c, now, status, _ := firecrackerIdleController(t)
	firecrackerToResumingAfterWake(t, r, c, now, status)
	reconcileResult(t, r)
	status.state.Phase = "running"
	reconcileResult(t, r)
	if got := getSandbox(t, c).Status.FirecrackerSuspension; got.Generation != 1 || got.Phase != sandboxapi.FirecrackerSuspendReleased {
		t.Fatalf("first suspension = %+v, want released generation 1", got)
	}

	sandbox := getSandbox(t, c)
	nextIdleSince := runnerIdleSince.Add(2 * time.Hour)
	if err := r.beginFirecrackerSuspend(context.Background(), sandbox, nextIdleSince); err != nil {
		t.Fatal(err)
	}
	if got := getSandbox(t, c); got.Status.FirecrackerSuspension == nil || got.Status.FirecrackerSuspension.Generation != 2 ||
		got.Status.FirecrackerSuspension.Phase != sandboxapi.FirecrackerSuspendPreparing || !got.Status.FirecrackerSuspension.IdleSince.Time.Equal(nextIdleSince) {
		t.Fatalf("next suspension = %+v, want preparing generation 2 at %s", got.Status.FirecrackerSuspension, nextIdleSince)
	}
}
