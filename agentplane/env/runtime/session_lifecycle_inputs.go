package runtime

import (
	"context"
	"errors"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
)

func (r *Runtime) publishSeal(id string, seal *journal.SealState) error {
	r.sealPublishMu.Lock()
	defer r.sealPublishMu.Unlock()
	value := journalSealSpec{}
	if seal != nil {
		value = journalSealSpec{Present: true, IncarnationUID: seal.IncarnationUID, Cursor: seal.Cursor}
	}
	return publishLifecycle(r, id, value)
}

func publishLifecycle[T lifecycleSpec[T]](r *Runtime, id string, value T) error {
	return cosi.Publish(r.lifecycleContext, r.lifecycleState, lifecycleResourceFor(id, value), value, lifecycle.EnvironmentOwner)
}

func (r *Runtime) admissionCount(ctx context.Context) (int, error) {
	input, found, err := lifecycleRead[environmentAdmissionCountSpec](ctx, r.lifecycleState, lifecycle.ResourceID)
	if err != nil {
		return 0, err
	}
	if !found {
		return 0, errors.New("Runtime admissions are not initialized")
	}
	return input.Admissions, nil
}

// Caller holds the Runtime admission lock: accounting must precede durable admission and serialize with closing.
func (r *Runtime) changeAdmissionsLocked(delta int) error {
	if delta < 0 && r.activeAdmissions < uint64(-delta) {
		return errors.New("Runtime admission count would become negative")
	}
	next := r.activeAdmissions
	if delta < 0 {
		next -= uint64(-delta)
	} else {
		next += uint64(delta)
	}
	value := environmentAdmissionCountSpec{Admissions: int(next)}
	if r.admissionOpen {
		value.AcceptedGeneration = r.desired.Generation
	}
	// Count publication is part of the synchronous admission gate. An uncertain
	// write must close admission even if the backing store committed it.
	_, err := safe.StateUpdateWithConflicts(r.lifecycleContext, r.lifecycleState, lifecycleResourceFor(lifecycle.ResourceID, value).Metadata(), func(current *typed.Resource[environmentAdmissionCountSpec, lifecycleExtension[environmentAdmissionCountSpec]]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner))
	if err != nil {
		r.admissionOpen = false
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = err
		}
		select {
		case r.fatal <- err:
		default:
		}
		return err
	}
	r.activeAdmissions = next
	return nil
}

func (r *Runtime) initializeSessionLifecycle() error {
	value := environmentAdmissionCountSpec{}
	if err := cosi.Publish(r.lifecycleContext, r.lifecycleState, lifecycleResourceFor(lifecycle.ResourceID, value), value, lifecycle.EnvironmentOwner); err != nil {
		return err
	}
	return r.lifecycleEngine.RegisterController(&sessionLifecycleController{runtime: r})
}
