package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// A transition is an ordered sequence of effect barriers. The Runtime lifecycle
// controller owns the sequence and its status; each blocking barrier has one worker.
// A completed or uncertain barrier is never replayed by controller restart.
type runtimeTransitionSequence struct {
	r         *Runtime
	sequence  uint64
	request   lifecycle.RuntimeTransitionRequest
	steps     []runtimeTransitionStep
	index     int
	worker    *runtimeTransitionWorker
	ctx       context.Context
	engineCtx context.Context
	cancel    context.CancelFunc
	terminal  bool
}

type runtimeTransitionStep struct {
	stage          lifecycle.RuntimeTransitionStage
	startExecution bool
	settleOnCancel func(context.Context) error
	run            func(context.Context) error
}

type runtimeTransitionWorker struct {
	done chan struct{}
	err  error
}

func (c *runtimeTransitionSequence) reconcile(ctx context.Context, engine controller.Runtime) error {
	request, found, err := lifecycle.ReadRuntimeTransitionRequest(ctx, engine)
	if err != nil || !found {
		return err
	}
	if c.sequence != request.Sequence {
		if c.worker != nil || c.sequence != 0 && !c.terminal {
			return fmt.Errorf("Runtime transition %d superseded before settlement", c.sequence)
		}
		c.sequence, c.request, c.terminal = request.Sequence, request, false
		c.steps, c.index = nil, -1
		c.r.mu.Lock()
		transitionCtx := c.r.activeTransitionCtx
		activeSequence := c.r.activeTransitionSequence
		c.r.mu.Unlock()
		if activeSequence != request.Sequence || transitionCtx == nil {
			return fmt.Errorf("Runtime transition %d has no admitted caller", request.Sequence)
		}
		c.ctx, c.cancel = context.WithCancel(transitionCtx)
		return c.start(ctx, engine, runtimeTransitionStep{stage: lifecycle.RuntimeTransitionPlanning, run: func(ctx context.Context) error {
			switch request.Kind {
			case lifecycle.RuntimeAdopt:
				steps, err := c.planAdoption(ctx, request.UID)
				if err == nil {
					c.steps = steps
				}
				return err
			case lifecycle.RuntimeQuiesce:
				steps, err := c.planQuiesce(ctx, request)
				if err == nil {
					c.steps = steps
				}
				return err
			default:
				return fmt.Errorf("unknown Runtime transition kind %d", request.Kind)
			}
		}})
	}
	if c.worker == nil || c.terminal {
		return nil
	}
	select {
	case <-c.worker.done:
	default:
		return nil
	}
	completed := c.worker
	c.worker = nil
	if completed.err != nil {
		c.terminal = true
		c.cancel()
		code := status.Code(completed.err)
		if errors.Is(completed.err, context.Canceled) {
			code = codes.Canceled
		} else if errors.Is(completed.err, context.DeadlineExceeded) {
			code = codes.DeadlineExceeded
		}
		return c.publish(ctx, engine, lifecycle.RuntimeTransitionFailed, completed.err.Error(), code)
	}
	c.index++
	if c.index >= len(c.steps) {
		c.terminal = true
		c.cancel()
		return c.publish(ctx, engine, lifecycle.RuntimeTransitionDone, "", codes.OK)
	}
	return c.start(ctx, engine, c.steps[c.index])
}

func (c *runtimeTransitionSequence) start(ctx context.Context, engine controller.Runtime, step runtimeTransitionStep) error {
	if err := c.publish(ctx, engine, step.stage, "", codes.OK); err != nil {
		return err
	}
	worker := &runtimeTransitionWorker{done: make(chan struct{})}
	c.worker = worker
	go func() {
		worker.err = c.runStep(step)
		close(worker.done)
	}()
	return nil
}

func (c *runtimeTransitionSequence) runStep(step runtimeTransitionStep) error {
	if err := c.ctx.Err(); err != nil {
		return err
	}
	err := step.run(c.ctx)
	if err != nil && step.stage == lifecycle.RuntimeTransitionExecutionFencing {
		if errors.Is(err, context.Canceled) && !c.r.isClosing() && step.settleOnCancel != nil {
			fenceCtx, cancel := context.WithTimeout(c.engineCtx, executionFenceGrace)
			defer cancel()
			if settleErr := step.settleOnCancel(fenceCtx); settleErr == nil {
				return err
			} else {
				err = errors.Join(err, fmt.Errorf("settle exact Execution fence after epoch loss: %w", settleErr))
			}
		}
		if !c.r.isClosing() {
			c.r.mu.Lock()
			c.r.transitionUnsafe = err
			c.r.mu.Unlock()
		}
		return err
	}
	if err == nil || !step.startExecution {
		return err
	}
	// startExecutionForUID can return on epoch cancellation while its COSI
	// startup is still in flight. The public stop barrier settles that start
	// even if no executionOwner handle has been published yet.
	fenceCtx, cancel := context.WithTimeout(c.engineCtx, executionFenceGrace)
	defer cancel()
	if fenceErr := c.r.stopExecution(fenceCtx); fenceErr != nil {
		unsafe := errors.Join(err, fmt.Errorf("fence interrupted Execution start: %w", fenceErr))
		c.r.mu.Lock()
		c.r.transitionUnsafe = unsafe
		c.r.mu.Unlock()
		return unsafe
	}
	return err
}

func (c *runtimeTransitionSequence) publish(ctx context.Context, engine controller.Runtime, stage lifecycle.RuntimeTransitionStage, failure string, errorCode codes.Code) error {
	if err := lifecycle.WriteRuntimeTransitionStatus(ctx, engine, lifecycle.RuntimeTransitionStatus{
		Sequence: c.sequence, Stage: stage, Error: failure, ErrorCode: uint32(errorCode),
	}); err != nil {
		return err
	}
	c.r.changed()
	return nil
}

func (c *runtimeTransitionSequence) planAdoption(ctx context.Context, uid string) ([]runtimeTransitionStep, error) {
	r := c.r
	r.mu.Lock()
	if r.closing || r.host == nil || !r.host.connected {
		r.mu.Unlock()
		return nil, status.Error(codes.Unavailable, "environment Runtime or Harness Host is closing")
	}
	oldUID, known := r.state.IncarnationUID()
	initialInactive := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE
	var initialBinding *runtimepb.Binding
	if initialInactive {
		initialBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	}
	r.mu.Unlock()
	if initialInactive {
		if err := r.waitForHostNativeStopped(ctx, initialBinding); err != nil {
			return nil, status.Errorf(codes.Unavailable, "initial Harness Host generation did not establish its native-stop barrier: %v", err)
		}
	}
	sealedUID, sealed, err := r.state.SealedUID()
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "read environment seal: %v", err)
	}
	if sealed {
		oldUID, known = sealedUID, true
	}
	var steps []runtimeTransitionStep
	add := func(stage lifecycle.RuntimeTransitionStage, start bool, run func(context.Context) error) {
		steps = append(steps, runtimeTransitionStep{stage: stage, startExecution: start, run: run})
	}
	addFence := func(binding **runtimepb.Binding, trigger bool) {
		add(lifecycle.RuntimeTransitionExecutionFencing, false, func(ctx context.Context) error {
			if trigger {
				r.maybeFenceExecution(*binding)
			}
			return r.waitForExecutionFenced(ctx, *binding)
		})
		steps[len(steps)-1].settleOnCancel = func(ctx context.Context) error {
			return r.waitForExecutionFenced(ctx, *binding)
		}
	}
	start := func(target string) {
		add(lifecycle.RuntimeTransitionExecutionStarting, true, func(ctx context.Context) error { return r.startExecutionForUID(ctx, target) })
	}
	ready := func(binding **runtimepb.Binding, message string) {
		add(lifecycle.RuntimeTransitionHostReady, false, func(ctx context.Context) error {
			_, err := r.waitForHostReady(ctx, *binding)
			if err != nil {
				return status.Errorf(codes.Unavailable, "%s: %v", message, err)
			}
			return nil
		})
	}
	bootstrap := func(binding **runtimepb.Binding) {
		add(lifecycle.RuntimeTransitionBootstrapRequesting, false, func(ctx context.Context) error {
			r.setLastRunning(*binding)
			return r.requestBootstrap(ctx)
		})
	}
	if known && oldUID == uid && !sealed {
		r.mu.Lock()
		alreadyRunning := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && r.intent.IncarnationUid == uid &&
			r.host != nil && r.host.report != nil && r.host.report.Ready && proto.Equal(r.host.report.Binding, r.intent.Binding)
		var binding *runtimepb.Binding
		mode := runtimepb.LifecycleIntent_INACTIVE
		if r.intent != nil {
			binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			mode = r.intent.Mode
		}
		r.mu.Unlock()
		if alreadyRunning {
			bootstrap(&binding)
			return steps, nil
		}
		if mode == runtimepb.LifecycleIntent_RUNNING {
			start(uid)
			ready(&binding, "Harness Host did not apply the active generation")
			bootstrap(&binding)
			return steps, nil
		}
		if mode == runtimepb.LifecycleIntent_QUIESCING || mode == runtimepb.LifecycleIntent_CLOSING || mode == runtimepb.LifecycleIntent_INACTIVE {
			add(lifecycle.RuntimeTransitionNativeStopping, false, func(ctx context.Context) error {
				if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
					return status.Errorf(codes.Unavailable, "prior Harness Host generation did not drain: %v", err)
				}
				return nil
			})
			if mode != runtimepb.LifecycleIntent_INACTIVE {
				addFence(&binding, true)
			}
		} else {
			return nil, status.Errorf(codes.Unavailable, "cannot resume environment from lifecycle mode %s", mode)
		}
		start(uid)
		add(lifecycle.RuntimeTransitionIntentPublishing, false, func(ctx context.Context) error {
			var err error
			binding, err = r.publishIntent(ctx, lifecycle.Running, runtimepb.LifecycleIntent_RUNNING, uid, "")
			return err
		})
		ready(&binding, "Harness Host did not apply the active generation")
		bootstrap(&binding)
		return steps, nil
	}
	if known {
		var binding *runtimepb.Binding
		mode, seal := runtimepb.LifecycleIntent_CLOSING, ""
		if sealed {
			mode, seal = runtimepb.LifecycleIntent_QUIESCING, oldUID
		}
		r.mu.Lock()
		prior := r.intent
		if prior != nil && prior.Mode == mode && prior.IncarnationUid == oldUID && prior.SealUid == seal {
			binding = proto.Clone(prior.Binding).(*runtimepb.Binding)
		}
		r.mu.Unlock()
		if binding == nil {
			start(oldUID)
			add(lifecycle.RuntimeTransitionIntentPublishing, false, func(ctx context.Context) error {
				var err error
				binding, err = r.publishIntent(ctx, lifecycle.Quiescing, mode, oldUID, seal)
				return err
			})
		}
		add(lifecycle.RuntimeTransitionNativeStopping, false, func(ctx context.Context) error {
			return r.waitForHostNativeStopped(ctx, binding)
		})
		addFence(&binding, true)
		if sealed {
			add(lifecycle.RuntimeTransitionHostAcknowledging, false, func(ctx context.Context) error {
				return r.waitForHostQuiesced(ctx, binding)
			})
		}
	}
	add(lifecycle.RuntimeTransitionIncarnationAdopting, false, func(ctx context.Context) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := r.state.AdoptIncarnation(uid); err != nil {
			return status.Errorf(codes.Unavailable, "adopt Sandbox incarnation: %v", err)
		}
		if sealed && oldUID == uid {
			return nil
		}
		start(uid)
		var binding *runtimepb.Binding
		if r.state.Lameduck() {
			add(lifecycle.RuntimeTransitionIntentPublishing, false, func(ctx context.Context) error {
				var err error
				binding, err = r.publishIntent(ctx, lifecycle.Quiescing, runtimepb.LifecycleIntent_QUIESCING, uid, uid)
				return err
			})
			add(lifecycle.RuntimeTransitionHostReady, false, func(ctx context.Context) error {
				report, err := r.waitForHostReady(ctx, binding)
				if err != nil {
					return status.Errorf(codes.Unavailable, "sealed Harness Host generation did not apply: %v", err)
				}
				if report.NativeStopped {
					r.maybeFenceExecution(binding)
				}
				return nil
			})
			add(lifecycle.RuntimeTransitionNativeStopping, false, func(ctx context.Context) error {
				return r.waitForHostNativeStopped(ctx, binding)
			})
			addFence(&binding, false)
			add(lifecycle.RuntimeTransitionHostAcknowledging, false, func(ctx context.Context) error {
				return r.waitForHostQuiesced(ctx, binding)
			})
		} else {
			add(lifecycle.RuntimeTransitionIntentPublishing, false, func(ctx context.Context) error {
				var err error
				binding, err = r.publishIntent(ctx, lifecycle.Running, runtimepb.LifecycleIntent_RUNNING, uid, "")
				return err
			})
			ready(&binding, "Harness Host did not apply the active generation")
			bootstrap(&binding)
		}
		c.steps = steps
		return nil
	})
	return steps, nil
}

func (c *runtimeTransitionSequence) planQuiesce(ctx context.Context, request lifecycle.RuntimeTransitionRequest) ([]runtimeTransitionStep, error) {
	r := c.r
	facts, err := r.facts()
	if err != nil {
		return nil, err
	}
	var uid string
	var current bool
	var host *hostLink
	var binding *runtimepb.Binding
	var currentReport bool
	var retry bool
	steps := []runtimeTransitionStep{{stage: lifecycle.RuntimeTransitionQuiesceSealing, run: func(ctx context.Context) error {
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			return errors.New("environment Runtime is closing")
		}
		if !r.state.Lameduck() && request.HasIdleSince &&
			(facts.IdleSince == nil || facts.IdleSince.AsTime().UnixMicro() != request.IdleSinceMicros ||
				r.activeAdmissions != 0 || r.activeExecutionCalls != 0 || r.executionRecovering ||
				r.host == nil || r.host.report == nil || !r.host.report.Ready || r.intent == nil ||
				!proto.Equal(r.host.report.Binding, r.intent.Binding) || r.host.report.Facts == nil ||
				r.host.report.Facts.IdleSince == nil ||
				r.host.report.Facts.IdleSince.AsTime().UnixMicro() != request.IdleSinceMicros) {
			return status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
		}
		if r.host == nil || !r.host.connected {
			return status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
		}
		if err := r.state.EnterLameduck(); err != nil {
			return status.Errorf(codes.Unavailable, "persist environment seal: %v", err)
		}
		var err error
		uid, err = r.state.RequireIncarnationUID()
		if err != nil {
			return status.Errorf(codes.FailedPrecondition, "%v", err)
		}
		current = r.desired.Mode == lifecycle.Quiescing && r.desired.SealUID == uid && r.intent != nil &&
			r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == uid
		if current {
			host = r.host
			binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			currentReport = host.report != nil && host.report.Ready && proto.Equal(host.report.Binding, binding)
		}
		retry = !current
		return nil
	}}, {stage: lifecycle.RuntimeTransitionHostStatusChecking, run: func(ctx context.Context) error {
		if !currentReport {
			return nil
		}
		bound, err := host.client.BoundContext(ctx, binding)
		if err != nil {
			return status.Errorf(codes.Unavailable, "authorize Harness Host quiesce status: %v", err)
		}
		hostStatus, err := host.client.GetStatus(bound, &sessionpb.GetStatusRequest{})
		if err != nil {
			return status.Errorf(codes.Unavailable, "read Harness Host quiesce status: %v", err)
		}
		retry = hostStatus.GetLifecycle().GetError() != ""
		return nil
	}}, {stage: lifecycle.RuntimeTransitionIntentPublishing, run: func(ctx context.Context) error {
		if !retry {
			return nil
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.closing {
			return errors.New("environment Runtime is closing")
		}
		if current && (r.host != host || r.intent == nil || !proto.Equal(r.intent.Binding, binding) ||
			r.desired.Mode != lifecycle.Quiescing || r.desired.SealUID != uid) {
			return nil
		}
		desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, SealUID: uid, Generation: r.desired.Generation + 1}
		if err := lifecycle.PublishEnvironmentDesired(context.Background(), r.lifecycleState, desired); err != nil {
			return status.Errorf(codes.Unavailable, "publish environment quiesce: %v", err)
		}
		r.desired = desired
		r.setIntentLocked(runtimepb.LifecycleIntent_QUIESCING, uid, uid, false)
		return nil
	}}}
	return steps, nil
}
