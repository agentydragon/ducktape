package residentruntime

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
)

type runtimeSlot struct {
	runtime *Runtime
	task    *residentSession
}

func (r *Runtime) newDriver(task *residentSession) *sessionDriver {
	return &sessionDriver{
		store: r.store, settings: r.settings,
		claimLease: r.claimLease, releaseLease: r.releaseLease,
		newExecutor: r.newExecutor, openCodeSession: r.openCodeSession,
		codeHost: r.codeHost, slot: &runtimeSlot{runtime: r, task: task},
	}
}

func (s *runtimeSlot) wake() <-chan struct{}  { return s.task.wake }
func (s *runtimeSlot) drain() <-chan struct{} { return s.runtime.draining }

func (s *runtimeSlot) isStopping() bool {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	return s.runtime.stopping
}

func (s *runtimeSlot) shutdownDeadline() time.Time {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	return s.runtime.shutdownDeadline
}

func (s *runtimeSlot) setOwner(owner sessionstore.Owner) {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	s.task.owner = &owner
}

func (s *runtimeSlot) setWaitingProvider(waiting bool) {
	s.runtime.mu.Lock()
	defer s.runtime.mu.Unlock()
	s.task.waitingProvider = waiting
}

func (s *runtimeSlot) acquireExecution(ctx context.Context) bool {
	return s.runtime.acquireExecution(ctx, s.task)
}

func (s *runtimeSlot) releaseExecution() { s.runtime.releaseExecution(s.task) }
