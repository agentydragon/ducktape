package runtime

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
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

// Host publishers serialize compound snapshots; COSI handles conflicts and no-op updates.
func publishLifecycle[T lifecycleSpec[T]](r *Runtime, id string, value T) error {
	return cosi.Publish(r.lifecycleContext, r.lifecycleState, lifecycleResourceFor(id, value), value, harnesshostlifecycle.EnvironmentOwner)
}

func (r *Runtime) admissionCount(ctx context.Context) (int, error) {
	input, err := safe.ReaderGet[*typed.Resource[environmentAdmissionCountSpec, lifecycleExtension[environmentAdmissionCountSpec]]](ctx, r.lifecycleState, lifecycleResourceFor(harnesshostlifecycle.ResourceID, environmentAdmissionCountSpec{}).Metadata())
	if err != nil {
		return 0, err
	}
	return input.TypedSpec().Admissions, nil
}

// Caller holds the Runtime admission lock: accounting must precede durable admission and serialize with closing.
func (r *Runtime) changeAdmissionsLocked(delta int) error {
	_, err := safe.StateUpdateWithConflicts(r.lifecycleContext, r.lifecycleState, lifecycleResourceFor(harnesshostlifecycle.ResourceID, environmentAdmissionCountSpec{}).Metadata(), func(value *typed.Resource[environmentAdmissionCountSpec, lifecycleExtension[environmentAdmissionCountSpec]]) error {
		input := value.TypedSpec()
		if input.Admissions+delta < 0 {
			return errors.New("Runtime admission count would become negative")
		}
		input.Admissions += delta
		return nil
	}, state.WithUpdateOwner(harnesshostlifecycle.EnvironmentOwner))
	return err
}
