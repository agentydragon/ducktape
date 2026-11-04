package runtime

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type adoptionBarrier struct {
	uid     string
	sealUID string
	mode    lifecycle.Mode
}
type adoptionPlan struct {
	targetUID  string
	generation uint64
	sealed     bool
	oldUID     string
}
type adoptionOutcome uint8

const (
	adoptionNoSuccessor adoptionOutcome = iota
	adoptionRunningSuccessor
)

func (c *runtimeTransitionSequence) runAdoptionBarrier(ctx context.Context, step runtimeTransitionStep) (runtimeTransitionOutcome, error) {
	r := c.r
	switch step.kind {
	case runtimeTransitionNativeStopping:
		return runtimeTransitionOutcome{}, r.waitForNativeStopped(ctx, c.adoption.generation)
	case runtimeTransitionExecutionFencing:
		return runtimeTransitionOutcome{}, r.waitForExecutionFenced(ctx, c.adoption.generation)
	case runtimeTransitionExecutionStarting:
		return runtimeTransitionOutcome{}, r.startExecutionForUID(ctx, step.adoption.uid)
	case runtimeTransitionDesiredPublishing:
		generation, err := r.publishDesired(ctx, step.adoption.mode, step.adoption.uid, step.adoption.sealUID)
		return runtimeTransitionOutcome{generation: generation}, err
	case runtimeTransitionBootstrapRequesting:
		return runtimeTransitionOutcome{}, r.requestBootstrap(ctx)
	case runtimeTransitionHistoryAcknowledging:
		return runtimeTransitionOutcome{}, r.waitSessionStage(ctx, lifecycle.Quiesced)
	case runtimeTransitionIncarnationAdopting:
		if err := ctx.Err(); err != nil {
			return runtimeTransitionOutcome{}, err
		}
		if err := r.state.AdoptIncarnation(c.adoption.targetUID); err != nil {
			return runtimeTransitionOutcome{}, err
		}
		if c.adoption.sealed && c.adoption.oldUID == c.adoption.targetUID {
			return runtimeTransitionOutcome{adoption: adoptionNoSuccessor}, nil
		}
		return runtimeTransitionOutcome{adoption: adoptionRunningSuccessor}, nil
	default:
		return runtimeTransitionOutcome{}, errors.New("unknown Runtime transition step kind for adoption")
	}
}

func (c *runtimeTransitionSequence) recordAdoptionOutcome(step runtimeTransitionStep, outcome runtimeTransitionOutcome) error {
	if outcome.generation != 0 {
		c.adoption.generation = outcome.generation
	}
	if step.kind == runtimeTransitionIncarnationAdopting && outcome.adoption == adoptionRunningSuccessor {
		uid := c.adoption.targetUID
		c.steps = append(
			c.steps,
			runtimeTransitionStep{kind: runtimeTransitionExecutionStarting, adoption: adoptionBarrier{uid: uid}},
			runtimeTransitionStep{kind: runtimeTransitionDesiredPublishing, adoption: adoptionBarrier{uid: uid, mode: lifecycle.Running}},
			runtimeTransitionStep{kind: runtimeTransitionBootstrapRequesting},
		)
	}
	return nil
}

func (c *runtimeTransitionSequence) planAdoption(ctx context.Context, uid string) (adoptionPlan, []runtimeTransitionStep, error) {
	r := c.r
	r.mu.Lock()
	if r.closing || r.failed || !r.startupComplete {
		r.mu.Unlock()
		return adoptionPlan{}, nil, status.Error(codes.Unavailable, "environment Runtime is unavailable")
	}
	desired := r.desired
	accepted := r.admissionOpen
	r.mu.Unlock()
	sealedUID, sealed, err := r.state.SealedUID()
	if err != nil {
		return adoptionPlan{}, nil, err
	}
	oldUID, known := r.state.IncarnationUID()
	if sealed {
		oldUID, known = sealedUID, true
	}
	plan := adoptionPlan{targetUID: uid, generation: desired.Generation, sealed: sealed, oldUID: oldUID}
	if known && oldUID == uid && !sealed && desired.Mode == lifecycle.Running {
		if !accepted {
			return plan, nil, status.Error(codes.Unavailable, "environment revision has not completed preparation")
		}
		return plan, []runtimeTransitionStep{{kind: runtimeTransitionBootstrapRequesting}}, nil
	}
	var steps []runtimeTransitionStep
	add := func(kind runtimeTransitionStepKind, barrier adoptionBarrier) {
		steps = append(steps, runtimeTransitionStep{kind: kind, adoption: barrier})
	}
	if desired.Mode == lifecycle.Inactive {
		if err := r.waitForNativeStopped(ctx, desired.Generation); err != nil {
			return plan, nil, err
		}
	}
	if known {
		seal := ""
		if sealed {
			seal = oldUID
		}
		if desired.Mode != lifecycle.Quiescing || desired.IncarnationUID != oldUID || desired.SealUID != seal {
			if sealed {
				add(runtimeTransitionExecutionStarting, adoptionBarrier{uid: oldUID})
			}
			add(runtimeTransitionDesiredPublishing, adoptionBarrier{uid: oldUID, sealUID: seal, mode: lifecycle.Quiescing})
		}
		add(runtimeTransitionNativeStopping, adoptionBarrier{})
		add(runtimeTransitionExecutionFencing, adoptionBarrier{})
		if sealed {
			add(runtimeTransitionHistoryAcknowledging, adoptionBarrier{})
		}
	}
	add(runtimeTransitionIncarnationAdopting, adoptionBarrier{})
	return plan, steps, nil
}
