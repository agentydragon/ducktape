package centralsampler

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"time"
)

func (r *Runtime) Wake(sessionID string) {
	r.wake(sessionID)
	r.signal()
}

func (r *Runtime) wake(sessionID string) {
	r.mu.Lock()
	if r.runCtx != nil && !r.stopping {
		r.launchLocked(sessionID)
	}
	r.mu.Unlock()
}

func (r *Runtime) Run(ctx context.Context, work <-chan struct{}) error {
	r.mu.Lock()
	if r.started {
		r.mu.Unlock()
		return errors.New("central sampler runtime already started")
	}
	r.started = true
	// A service shutdown stops new work immediately, but an already dispatched effect must
	// retain its context long enough to reach a durable result or the drain deadline.
	r.runCtx, r.cancel = context.WithCancel(context.WithoutCancel(ctx))
	codeHost := r.codeHost
	r.mu.Unlock()

	recoveryDone := make(chan struct{})
	recoveryCtx, stopRecovery := context.WithCancel(r.runCtx)
	go func() {
		defer close(recoveryDone)
		r.recover(recoveryCtx, work)
	}()
	<-ctx.Done()
	shutdownAt := time.Now()
	// The chart grants 60 seconds and the default budget reserves five seconds
	// for the process to return after all drivers and the shared host stop.
	cleanupDeadline := shutdownAt.Add(r.settings.ShutdownBudgetTimeout())
	r.mu.Lock()
	r.stopping = true
	r.shutdownDeadline = cleanupDeadline
	close(r.draining)
	activeDrivers := len(r.tasks)
	r.mu.Unlock()
	codeHost.StopAccepting()
	slog.Info("central sampler drain started", "deadline", r.settings.ShutdownDrainTimeout(), "active_drivers", activeDrivers, "pod_name", os.Getenv("HOSTNAME"))
	stopRecovery()
	<-recoveryDone
	// A forced stop retains any unresolved intent and its raw evidence. Drivers join
	// before ownership is released, so a successor cannot race a live old writer.
	drained := make(chan struct{})
	go func() {
		r.drivers.Wait()
		close(drained)
	}()
	deadline := time.NewTimer(time.Until(shutdownAt.Add(r.settings.ShutdownDrainTimeout())))
	select {
	case <-drained:
		deadline.Stop()
	case <-deadline.C:
		slog.Warn("central sampler drain deadline expired; canceling active effects")
		r.cancel()
		<-drained
	}
	r.cancel()
	// Drivers deliberately use independent bounded contexts for Claim and Release. Waiting here
	// closes the committed-but-not-returned Claim and blocked Release shutdown races.
	closeCtx, closeCancel := context.WithDeadline(context.Background(), cleanupDeadline)
	if err := codeHost.Close(closeCtx); err != nil {
		slog.Error("central sampler code-mode host close failed", "error", err)
	}
	closeCancel()
	return nil
}

func (r *Runtime) recover(ctx context.Context, work <-chan struct{}) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		excluded := make([]string, 0, len(r.tasks))
		tokens := make([]string, 0, len(r.tasks))
		for sessionID, task := range r.tasks {
			excluded = append(excluded, sessionID)
			if task.owner != nil {
				tokens = append(tokens, task.owner.Token)
			}
		}
		r.mu.Unlock()
		owned, err := r.store.OwnedWork(ctx, tokens)
		if err != nil && ctx.Err() == nil {
			slog.Error("central sampler owned work scan failed", "error", err)
		} else {
			for _, sessionID := range owned {
				r.wake(sessionID)
			}
		}
		r.mu.Lock()
		capacity := r.settings.MaxLiveSessions - r.busyLocked()
		r.mu.Unlock()
		if capacity > 0 {
			sessions, err := r.store.RecoverableSessions(ctx, excluded, capacity, r.codeHost.InstanceID())
			if err != nil && ctx.Err() == nil {
				slog.Error("central sampler recovery scan failed", "error", err)
			} else {
				for _, sessionID := range sessions {
					r.wake(sessionID)
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-r.changed:
		case <-work:
		case <-ticker.C:
		}
	}
}

func (r *Runtime) launchLocked(sessionID string) {
	if sessionID == "" || r.runCtx == nil || r.stopping {
		return
	}
	if task, exists := r.tasks[sessionID]; exists {
		task.busy = true
		task.signal()
		return
	}
	if r.busyLocked() >= r.settings.MaxLiveSessions {
		return
	}
	task := &residentSession{wake: make(chan struct{}, 1), busy: true}
	r.tasks[sessionID] = task
	r.drivers.Add(1)
	go func() {
		defer r.drivers.Done()
		r.drive(r.runCtx, sessionID, task)
		r.mu.Lock()
		delete(r.tasks, sessionID)
		r.mu.Unlock()
		r.signal()
	}()
}

func (r *Runtime) busyLocked() int {
	count := 0
	for _, task := range r.tasks {
		if task.busy {
			count++
		}
	}
	return count
}

func (r *Runtime) acquireExecution(ctx context.Context, task *residentSession) bool {
	r.mu.Lock()
	task.busy = true
	r.mu.Unlock()
	select {
	case <-ctx.Done():
		return false
	case <-r.draining:
		return false
	case r.executing <- struct{}{}:
		return true
	}
}

func (r *Runtime) releaseExecution(task *residentSession) {
	<-r.executing
	r.mu.Lock()
	task.busy = false
	r.mu.Unlock()
	r.signal()
}

func (r *Runtime) signal() {
	select {
	case r.changed <- struct{}{}:
	default:
	}
}
