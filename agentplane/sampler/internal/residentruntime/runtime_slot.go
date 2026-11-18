package residentruntime

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
)

type runtimeSlot struct {
	runtime *Runtime
	task    *residentSession
}

func (r *Runtime) newDriver(task *residentSession) *session.Driver {
	return session.New(session.Dependencies{
		Store: r.store, Settings: r.settings,
		ClaimLease: r.claimLease, ReleaseLease: r.releaseLease,
		NewExecutor: r.newExecutor, OpenCodeSession: r.openCodeSession,
		CodeHost: r.codeHost,
	}, &runtimeSlot{runtime: r, task: task})
}

func (s *runtimeSlot) Wake() <-chan struct{}  { return s.task.wake }
func (s *runtimeSlot) Drain() <-chan struct{} { return s.runtime.draining }

func (s *runtimeSlot) IsStopping() bool {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	return s.runtime.stopping
}

func (s *runtimeSlot) ShutdownDeadline() time.Time {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	return s.runtime.shutdownDeadline
}

func (s *runtimeSlot) SetOwner(owner sessionstore.Owner) {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	s.task.owner = &owner
}

func (s *runtimeSlot) SetWaitingProvider(waiting bool) {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	s.task.waitingProvider = waiting
}

func (s *runtimeSlot) AcquireExecution(ctx context.Context) bool {
	return s.runtime.acquireExecution(ctx, s.task)
}

func (s *runtimeSlot) ReleaseExecution() { s.runtime.releaseExecution(s.task) }
