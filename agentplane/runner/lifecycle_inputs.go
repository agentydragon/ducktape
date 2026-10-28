package runner

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
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
	return safe.StateModify(l.runner.ctx, l.state, lifecycleResourceFor(id, value), func(r *typed.Resource[T, lifecycleExtension[T]]) error {
		*r.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner("runner-host"))
}

func (l *lifecycle) admissionCount(ctx context.Context) (int, error) {
	input, found, err := lifecycleRead[runnerInputSpec](ctx, l.state, "runner")
	if err != nil {
		return 0, err
	}
	if !found {
		return 0, errors.New("runner admission state is missing")
	}
	return input.Admissions, nil
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
