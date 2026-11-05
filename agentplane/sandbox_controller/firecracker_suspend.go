package sandboxcontroller

import (
	"context"
	"fmt"
	"math"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// RuntimeSuspender fences admission without sealing Sessions or stopping processes.
// Generations survive controller restarts and distinguish delayed calls from a later attempt.
type RuntimeSuspender interface {
	Prepare(context.Context, *sandboxapi.Sandbox, int64, time.Time) error
	Resume(context.Context, *sandboxapi.Sandbox, int64) error
}

type GatewaySuspender struct {
	Target    string
	TokenFile string
}

func (g GatewaySuspender) withClient(ctx context.Context, sandbox *sandboxapi.Sandbox, call func(context.Context, environmentruntimepb.EnvironmentRuntimeClient) error) error {
	return environmentruntimeclient.WithClient(ctx, environmentruntimeclient.Config{
		Target:    environmentruntimeclient.ExpandTarget(g.Target, sandbox.Name, sandbox.Namespace),
		TokenFile: g.TokenFile, Timeout: quiesceTimeout,
		SandboxName: sandbox.Name, SandboxUID: string(sandbox.UID), SandboxNamespace: sandbox.Namespace,
	}, call)
}

func (g GatewaySuspender) Prepare(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64, idleSince time.Time) error {
	return g.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.PrepareSuspend(ctx, &environmentruntimepb.PrepareSuspendRequest{
			Generation: generation, IfIdleSince: timestamppb.New(idleSince),
		})
		return err
	})
}

func (g GatewaySuspender) Resume(ctx context.Context, sandbox *sandboxapi.Sandbox, generation int64) error {
	return g.withClient(ctx, sandbox, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.ResumeAdmission(ctx, &environmentruntimepb.ResumeAdmissionRequest{Generation: generation})
		return err
	})
}

func firecrackerSuspending(sandbox *sandboxapi.Sandbox) bool {
	attempt := sandbox.Status.FirecrackerSuspension
	return attempt != nil && attempt.Phase != sandboxapi.FirecrackerSuspendReleased
}

func (r *Reconciler) beginFirecrackerSuspend(ctx context.Context, sandbox *sandboxapi.Sandbox, idleSince time.Time) error {
	if r.Suspender == nil {
		return fmt.Errorf("Firecracker admission suspender is not configured")
	}
	generation := int64(1)
	if previous := sandbox.Status.FirecrackerSuspension; previous != nil {
		if previous.Generation == math.MaxInt64 {
			return fmt.Errorf("Firecracker suspension generation is exhausted")
		}
		generation = previous.Generation + 1
	}
	sandbox.Status.FirecrackerSuspension = &sandboxapi.FirecrackerSuspension{
		Generation: generation, IdleSince: metav1.NewMicroTime(idleSince), Phase: sandboxapi.FirecrackerSuspendPreparing,
	}
	idle, wake := sandboxapi.SuspendedByIdle, sandbox.Spec.WakeGeneration
	sandbox.Status.SuspendedBy, sandbox.Status.SuspendedAtWakeGeneration = &idle, &wake
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	// Fence Gateway dispatch and persist the retry identity before the RPC. A lost
	// reply or concurrent wake must not abandon an acknowledged Runtime barrier.
	return r.Status().Update(ctx, sandbox)
}

func (r *Reconciler) writeFirecrackerSuspendPhase(ctx context.Context, sandbox *sandboxapi.Sandbox, phase sandboxapi.FirecrackerSuspensionPhase) (*unready, error) {
	sandbox.Status.FirecrackerSuspension.Phase = phase
	sandbox.Status.OperatingMode = sandboxOperatingMode(sandbox)
	if err := r.Status().Update(ctx, sandbox); err != nil {
		return nil, err
	}
	return &unready{reason: "FirecrackerPending", message: "waiting for the RAM suspension admission barrier", retryAfter: 2 * time.Second}, nil
}

func (r *Reconciler) convergeFirecrackerSuspension(ctx context.Context, sandbox *sandboxapi.Sandbox, pod *corev1.Pod) (*unready, error) {
	if r.Suspender == nil {
		return &unready{reason: "FirecrackerPending", message: "no Runtime admission suspender configured", retryAfter: 5 * time.Second}, nil
	}
	attempt := sandbox.Status.FirecrackerSuspension
	switch attempt.Phase {
	case sandboxapi.FirecrackerSuspendPreparing:
		err := r.Suspender.Prepare(ctx, sandbox, attempt.Generation, attempt.IdleSince.Time)
		if status.Code(err) == codes.Aborted {
			// Busy/stale idle facts do not authorize a checkpoint. Resume tombstones
			// the generation before dispatch reopens, including a delayed Prepare.
			sandbox.Status.SuspendedBy, sandbox.Status.SuspendedAtWakeGeneration = nil, nil
			r.idle.forget(client.ObjectKeyFromObject(sandbox))
			return r.writeFirecrackerSuspendPhase(ctx, sandbox, sandboxapi.FirecrackerSuspendResuming)
		}
		if err != nil {
			return &unready{reason: "FirecrackerPending", message: "waiting for Runtime suspend admission: " + err.Error(), retryAfter: 5 * time.Second}, nil
		}
		return r.writeFirecrackerSuspendPhase(ctx, sandbox, sandboxapi.FirecrackerSuspendCheckpointing)
	case sandboxapi.FirecrackerSuspendCheckpointing:
		blocked, err := r.convergeFirecrackerIntent(ctx, sandbox, pod, false)
		if err != nil || blocked == nil || blocked.stage != sandboxapi.StopStageStopped || !sandboxRunning(sandbox) {
			return blocked, err
		}
		// Even a wake received before intent projection waits for the actual
		// checkpoint, so the admission fence cannot reopen ahead of a late freeze.
		return r.writeFirecrackerSuspendPhase(ctx, sandbox, sandboxapi.FirecrackerSuspendResuming)
	case sandboxapi.FirecrackerSuspendResuming:
		blocked, err := r.convergeFirecrackerIntent(ctx, sandbox, pod, true)
		if err != nil || blocked != nil {
			return blocked, err
		}
		if err := r.Suspender.Resume(ctx, sandbox, attempt.Generation); err != nil {
			return &unready{reason: "FirecrackerPending", message: "waiting for Runtime resume admission: " + err.Error(), retryAfter: 5 * time.Second}, nil
		}
		return r.writeFirecrackerSuspendPhase(ctx, sandbox, sandboxapi.FirecrackerSuspendReleased)
	default:
		return nil, fmt.Errorf("unknown Firecracker suspension phase %q", attempt.Phase)
	}
}
