package harnesshost

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/journal"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

func (l *lifecycle) publishSeal(id string, seal *journal.SealState) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := journalSealSpec{}
	if seal != nil {
		value = journalSealSpec{Present: true, IncarnationUID: seal.IncarnationUID, Cursor: seal.Cursor}
	}
	return publishLifecycle(l, id, value)
}

// Host publishers serialize compound snapshots; COSI handles conflicts and no-op updates.
func publishLifecycle[T lifecycleSpec[T]](l *lifecycle, id string, value T) error {
	return cosi.Publish(l.host.ctx, l.state, lifecycleResourceFor(id, value), value, harnesshostlifecycle.HostOwner)
}

func (l *lifecycle) admissionCount(ctx context.Context) (int, error) {
	input, err := safe.ReaderGet[*typed.Resource[harnessHostAdmissionCountSpec, lifecycleExtension[harnessHostAdmissionCountSpec]]](ctx, l.state, lifecycleResourceFor(harnesshostlifecycle.ResourceID, harnessHostAdmissionCountSpec{}).Metadata())
	if err != nil {
		return 0, err
	}
	return input.TypedSpec().Admissions, nil
}

// Caller holds the Harness Host admission lock: accounting must precede durable admission and serialize with closing.
func (l *lifecycle) changeAdmissionsLocked(delta int) error {
	_, err := safe.StateUpdateWithConflicts(l.host.ctx, l.state, lifecycleResourceFor(harnesshostlifecycle.ResourceID, harnessHostAdmissionCountSpec{}).Metadata(), func(value *typed.Resource[harnessHostAdmissionCountSpec, lifecycleExtension[harnessHostAdmissionCountSpec]]) error {
		input := value.TypedSpec()
		if input.Admissions+delta < 0 {
			return errors.New("Harness Host admission count would become negative")
		}
		input.Admissions += delta
		return nil
	}, state.WithUpdateOwner(harnesshostlifecycle.HostOwner))
	return err
}
