package runner

import (
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

func (l *lifecycle) publishAdmissionState() {
	l.runner.mu.Lock()
	admissions := l.runner.admissions
	defer l.runner.mu.Unlock()
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[runnerInputSpec](l.runner.ctx, l.state, "runner")
	if err != nil {
		l.runner.fail(err)
		return
	}
	input.Admissions = admissions
	input.Revision++
	l.runner.fail(publishLifecycle(l, "runner", input))
}
