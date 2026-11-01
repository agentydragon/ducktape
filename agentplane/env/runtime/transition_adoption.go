package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Adoption barriers carry their inputs as values. The only changing bindings
// are the exact generations returned by PublishIntent, recorded after its
// worker completes and before the next barrier starts.
type adoptionBarrierKind uint8

const (
	adoptionNativeStop adoptionBarrierKind = iota + 1
	adoptionExecutionFence
	adoptionExecutionStart
	adoptionPublishIntent
	adoptionHostReady
	adoptionBootstrap
	adoptionHostAck
	adoptionIncarnation
)

type adoptionBindingSlot uint8

const (
	adoptionPriorBinding adoptionBindingSlot = iota + 1
	adoptionCurrentBinding
)

type adoptionBarrier struct {
	kind                 adoptionBarrierKind
	binding              adoptionBindingSlot
	uid                  string
	sealUID              string
	desiredMode          lifecycle.Mode
	intentMode           runtimepb.LifecycleIntent_Mode
	triggerFence         bool
	fenceIfNativeStopped bool
	failureMessage       string
}

type adoptionPlan struct {
	priorBinding   *runtimepb.Binding
	currentBinding *runtimepb.Binding
	oldUID         string
	targetUID      string
	sealed         bool
}

type adoptionOutcome uint8

const (
	adoptionNoSuccessor adoptionOutcome = iota + 1
	adoptionRunningSuccessor
	adoptionSealedSuccessor
)

func (c *runtimeTransitionSequence) binding(slot adoptionBindingSlot) *runtimepb.Binding {
	switch slot {
	case adoptionPriorBinding:
		return c.adoption.priorBinding
	case adoptionCurrentBinding:
		return c.adoption.currentBinding
	default:
		return nil
	}
}

func (c *runtimeTransitionSequence) recordAdoptionOutcome(step runtimeTransitionStep, outcome runtimeTransitionOutcome) error {
	switch step.adoption.kind {
	case adoptionPublishIntent:
		if outcome.binding == nil {
			return errors.New("Runtime intent publication returned no binding")
		}
		switch step.adoption.binding {
		case adoptionPriorBinding:
			c.adoption.priorBinding = outcome.binding
		case adoptionCurrentBinding:
			c.adoption.currentBinding = outcome.binding
		default:
			return fmt.Errorf("unknown Runtime adoption binding slot %d", step.adoption.binding)
		}
	case adoptionIncarnation:
		if outcome.adoption < adoptionNoSuccessor || outcome.adoption > adoptionSealedSuccessor {
			return fmt.Errorf("unknown Runtime adoption outcome %d", outcome.adoption)
		}
		c.steps = append(c.steps, c.successorBarriers(outcome.adoption)...)
	}
	return nil
}

func (c *runtimeTransitionSequence) runAdoptionBarrier(ctx context.Context, step adoptionBarrier) (runtimeTransitionOutcome, error) {
	r := c.r
	binding := c.binding(step.binding)
	switch step.kind {
	case adoptionNativeStop:
		if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
			if step.failureMessage != "" {
				return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "%s: %v", step.failureMessage, err)
			}
			return runtimeTransitionOutcome{}, err
		}
	case adoptionExecutionFence:
		if step.triggerFence {
			r.maybeFenceExecution(binding)
		}
		return runtimeTransitionOutcome{}, r.waitForExecutionFenced(ctx, binding)
	case adoptionExecutionStart:
		return runtimeTransitionOutcome{}, r.startExecutionForUID(ctx, step.uid)
	case adoptionPublishIntent:
		published, err := r.publishIntent(ctx, step.desiredMode, step.intentMode, step.uid, step.sealUID)
		return runtimeTransitionOutcome{binding: published}, err
	case adoptionHostReady:
		report, err := r.waitForHostReady(ctx, binding)
		if err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "%s: %v", step.failureMessage, err)
		}
		if step.fenceIfNativeStopped && report.NativeStopped {
			r.maybeFenceExecution(binding)
		}
	case adoptionBootstrap:
		r.setLastRunning(binding)
		return runtimeTransitionOutcome{}, r.requestBootstrap(ctx)
	case adoptionHostAck:
		return runtimeTransitionOutcome{}, r.waitForHostQuiesced(ctx, binding)
	case adoptionIncarnation:
		if err := ctx.Err(); err != nil {
			return runtimeTransitionOutcome{}, err
		}
		if err := r.state.AdoptIncarnation(c.adoption.targetUID); err != nil {
			return runtimeTransitionOutcome{}, status.Errorf(codes.Unavailable, "adopt Sandbox incarnation: %v", err)
		}
		if c.adoption.sealed && c.adoption.oldUID == c.adoption.targetUID {
			return runtimeTransitionOutcome{adoption: adoptionNoSuccessor}, nil
		}
		if r.state.Lameduck() {
			return runtimeTransitionOutcome{adoption: adoptionSealedSuccessor}, nil
		}
		return runtimeTransitionOutcome{adoption: adoptionRunningSuccessor}, nil
	default:
		return runtimeTransitionOutcome{}, fmt.Errorf("unknown Runtime adoption barrier %d", step.kind)
	}
	return runtimeTransitionOutcome{}, nil
}

func (c *runtimeTransitionSequence) successorBarriers(outcome adoptionOutcome) []runtimeTransitionStep {
	uid := c.adoption.targetUID
	switch outcome {
	case adoptionNoSuccessor:
		return nil
	case adoptionRunningSuccessor:
		return []runtimeTransitionStep{
			{stage: lifecycle.RuntimeTransitionExecutionStarting, adoption: adoptionBarrier{kind: adoptionExecutionStart, uid: uid}},
			{stage: lifecycle.RuntimeTransitionIntentPublishing, adoption: adoptionBarrier{kind: adoptionPublishIntent, binding: adoptionCurrentBinding, uid: uid, desiredMode: lifecycle.Running, intentMode: runtimepb.LifecycleIntent_RUNNING}},
			{stage: lifecycle.RuntimeTransitionHostReady, adoption: adoptionBarrier{kind: adoptionHostReady, binding: adoptionCurrentBinding, failureMessage: "Harness Host did not apply the active generation"}},
			{stage: lifecycle.RuntimeTransitionBootstrapRequesting, adoption: adoptionBarrier{kind: adoptionBootstrap, binding: adoptionCurrentBinding}},
		}
	case adoptionSealedSuccessor:
		return []runtimeTransitionStep{
			{stage: lifecycle.RuntimeTransitionExecutionStarting, adoption: adoptionBarrier{kind: adoptionExecutionStart, uid: uid}},
			{stage: lifecycle.RuntimeTransitionIntentPublishing, adoption: adoptionBarrier{kind: adoptionPublishIntent, binding: adoptionCurrentBinding, uid: uid, sealUID: uid, desiredMode: lifecycle.Quiescing, intentMode: runtimepb.LifecycleIntent_QUIESCING}},
			{stage: lifecycle.RuntimeTransitionHostReady, adoption: adoptionBarrier{kind: adoptionHostReady, binding: adoptionCurrentBinding, failureMessage: "sealed Harness Host generation did not apply", fenceIfNativeStopped: true}},
			{stage: lifecycle.RuntimeTransitionNativeStopping, adoption: adoptionBarrier{kind: adoptionNativeStop, binding: adoptionCurrentBinding}},
			{stage: lifecycle.RuntimeTransitionExecutionFencing, adoption: adoptionBarrier{kind: adoptionExecutionFence, binding: adoptionCurrentBinding}},
			{stage: lifecycle.RuntimeTransitionHostAcknowledging, adoption: adoptionBarrier{kind: adoptionHostAck, binding: adoptionCurrentBinding}},
		}
	default:
		return nil
	}
}

func (c *runtimeTransitionSequence) planAdoption(ctx context.Context, uid string) (adoptionPlan, []runtimeTransitionStep, error) {
	r := c.r
	r.mu.Lock()
	if r.closing || r.host == nil || !r.host.connected {
		r.mu.Unlock()
		return adoptionPlan{}, nil, status.Error(codes.Unavailable, "environment Runtime or Harness Host is closing")
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
			return adoptionPlan{}, nil, status.Errorf(codes.Unavailable, "initial Harness Host generation did not establish its native-stop barrier: %v", err)
		}
	}
	sealedUID, sealed, err := r.state.SealedUID()
	if err != nil {
		return adoptionPlan{}, nil, status.Errorf(codes.Unavailable, "read environment seal: %v", err)
	}
	if sealed {
		oldUID, known = sealedUID, true
	}
	plan := adoptionPlan{oldUID: oldUID, targetUID: uid, sealed: sealed}
	steps := make([]runtimeTransitionStep, 0, 12)
	add := func(stage lifecycle.RuntimeTransitionStage, barrier adoptionBarrier) {
		steps = append(steps, runtimeTransitionStep{stage: stage, adoption: barrier})
	}
	if known && oldUID == uid && !sealed {
		r.mu.Lock()
		report := r.hostReportLocked()
		alreadyRunning := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && r.intent.IncarnationUid == uid &&
			r.host != nil && report != nil && report.Ready && proto.Equal(report.Binding, r.intent.Binding)
		mode := runtimepb.LifecycleIntent_INACTIVE
		if r.intent != nil {
			plan.priorBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			plan.currentBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			mode = r.intent.Mode
		}
		r.mu.Unlock()
		if alreadyRunning {
			add(lifecycle.RuntimeTransitionBootstrapRequesting, adoptionBarrier{kind: adoptionBootstrap, binding: adoptionCurrentBinding})
			return plan, steps, nil
		}
		if mode == runtimepb.LifecycleIntent_RUNNING {
			add(lifecycle.RuntimeTransitionExecutionStarting, adoptionBarrier{kind: adoptionExecutionStart, uid: uid})
			add(lifecycle.RuntimeTransitionHostReady, adoptionBarrier{kind: adoptionHostReady, binding: adoptionCurrentBinding, failureMessage: "Harness Host did not apply the active generation"})
			add(lifecycle.RuntimeTransitionBootstrapRequesting, adoptionBarrier{kind: adoptionBootstrap, binding: adoptionCurrentBinding})
			return plan, steps, nil
		}
		if mode != runtimepb.LifecycleIntent_QUIESCING && mode != runtimepb.LifecycleIntent_CLOSING && mode != runtimepb.LifecycleIntent_INACTIVE {
			return adoptionPlan{}, nil, status.Errorf(codes.Unavailable, "cannot resume environment from lifecycle mode %s", mode)
		}
		add(lifecycle.RuntimeTransitionNativeStopping, adoptionBarrier{kind: adoptionNativeStop, binding: adoptionPriorBinding, failureMessage: "prior Harness Host generation did not drain"})
		if mode != runtimepb.LifecycleIntent_INACTIVE {
			add(lifecycle.RuntimeTransitionExecutionFencing, adoptionBarrier{kind: adoptionExecutionFence, binding: adoptionPriorBinding, triggerFence: true})
		}
		add(lifecycle.RuntimeTransitionExecutionStarting, adoptionBarrier{kind: adoptionExecutionStart, uid: uid})
		add(lifecycle.RuntimeTransitionIntentPublishing, adoptionBarrier{kind: adoptionPublishIntent, binding: adoptionCurrentBinding, uid: uid, desiredMode: lifecycle.Running, intentMode: runtimepb.LifecycleIntent_RUNNING})
		add(lifecycle.RuntimeTransitionHostReady, adoptionBarrier{kind: adoptionHostReady, binding: adoptionCurrentBinding, failureMessage: "Harness Host did not apply the active generation"})
		add(lifecycle.RuntimeTransitionBootstrapRequesting, adoptionBarrier{kind: adoptionBootstrap, binding: adoptionCurrentBinding})
		return plan, steps, nil
	}
	if known {
		mode, seal := runtimepb.LifecycleIntent_CLOSING, ""
		if sealed {
			mode, seal = runtimepb.LifecycleIntent_QUIESCING, oldUID
		}
		r.mu.Lock()
		prior := r.intent
		if prior != nil && prior.Mode == mode && prior.IncarnationUid == oldUID && prior.SealUid == seal {
			plan.priorBinding = proto.Clone(prior.Binding).(*runtimepb.Binding)
		}
		r.mu.Unlock()
		if plan.priorBinding == nil {
			add(lifecycle.RuntimeTransitionExecutionStarting, adoptionBarrier{kind: adoptionExecutionStart, uid: oldUID})
			add(lifecycle.RuntimeTransitionIntentPublishing, adoptionBarrier{kind: adoptionPublishIntent, binding: adoptionPriorBinding, uid: oldUID, sealUID: seal, desiredMode: lifecycle.Quiescing, intentMode: mode})
		}
		add(lifecycle.RuntimeTransitionNativeStopping, adoptionBarrier{kind: adoptionNativeStop, binding: adoptionPriorBinding})
		add(lifecycle.RuntimeTransitionExecutionFencing, adoptionBarrier{kind: adoptionExecutionFence, binding: adoptionPriorBinding, triggerFence: true})
		if sealed {
			add(lifecycle.RuntimeTransitionHostAcknowledging, adoptionBarrier{kind: adoptionHostAck, binding: adoptionPriorBinding})
		}
	}
	add(lifecycle.RuntimeTransitionIncarnationAdopting, adoptionBarrier{kind: adoptionIncarnation})
	return plan, steps, nil
}
