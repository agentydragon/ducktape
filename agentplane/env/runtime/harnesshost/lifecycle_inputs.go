package harnesshost

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
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

func (l *lifecycle) publishAck(id string, ack retained.Acknowledgement) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	return publishLifecycle(l, id, ingressAckSpec{Cursor: ack.Cursor, Sealed: ack.Sealed})
}

// Host publishers serialize compound snapshots; COSI handles conflicts and no-op updates.
func publishLifecycle[T lifecycleSpec[T]](l *lifecycle, id string, value T) error {
	return cosi.Publish(l.runner.ctx, l.state, lifecycleResourceFor(id, value), value, "runner-host")
}

func (l *lifecycle) admissionCount(ctx context.Context) (int, error) {
	input, err := safe.ReaderGet[*typed.Resource[runnerInputSpec, lifecycleExtension[runnerInputSpec]]](ctx, l.state, lifecycleResourceFor("runner", runnerInputSpec{}).Metadata())
	if err != nil {
		return 0, err
	}
	return input.TypedSpec().Admissions, nil
}

// Caller holds Runner.mu: accounting must precede durable admission and serialize with closing.
func (l *lifecycle) changeAdmissionsLocked(delta int) error {
	_, err := safe.StateUpdateWithConflicts(l.runner.ctx, l.state, lifecycleResourceFor("runner", runnerInputSpec{}).Metadata(), func(value *typed.Resource[runnerInputSpec, lifecycleExtension[runnerInputSpec]]) error {
		input := value.TypedSpec()
		if input.Admissions+delta < 0 {
			return errors.New("runner admission count would become negative")
		}
		input.Admissions += delta
		return nil
	}, state.WithUpdateOwner("runner-host"))
	return err
}
