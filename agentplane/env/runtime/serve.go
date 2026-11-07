package runtime

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/ingress"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"google.golang.org/grpc"
)

const sessionStartupTimeout = 90 * time.Second

// Run starts the Session components and Gateway-facing control server.
// Runtime owns the environment lifecycle and management-process lifetime.
func (r *Runtime) Run(ctx context.Context) error {
	if err := r.startSessions(); err != nil {
		r.lifecycleStartMu.Lock()
		unsafe := r.lifecycleInitErr != nil
		r.lifecycleStartMu.Unlock()
		if unsafe {
			// No Session native work has started. The partial controller set cannot
			// be run safely; retain the environment owner for process-level fencing.
			return fmt.Errorf("start Sessions: %w", err)
		}
		return errors.Join(fmt.Errorf("start Sessions: %w", err), r.Close(context.Background()))
	}
	startupCtx, cancelStartup := context.WithTimeout(ctx, sessionStartupTimeout)
	err := r.waitForInitialSessions(startupCtx)
	cancelStartup()
	if err != nil {
		return errors.Join(fmt.Errorf("wait for initial Sessions drain: %w", err), r.Close(context.Background()))
	}

	publicListener, publicServer, err := r.control.Listen(r.executorProxy)
	if err != nil {
		return errors.Join(fmt.Errorf("listen for Gateway control: %w", err), r.Close(context.Background()))
	}
	publicServeErr := make(chan error, 1)
	go func() {
		if err := publicServer.Serve(publicListener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			publicServeErr <- err
		}
	}()

	var runErr error
	select {
	case <-ctx.Done():
	case runErr = <-r.fatal:
	case runErr = <-publicServeErr:
	}
	publicServer.Stop()
	_ = publicListener.Close()
	closeErr := r.Close(context.Background())
	return errors.Join(runErr, closeErr)
}

func (r *Runtime) startSessions() error {
	r.lifecycleStartMu.Lock()
	defer r.lifecycleStartMu.Unlock()
	r.mu.Lock()
	closing := r.closing
	r.mu.Unlock()
	if closing {
		return errors.New("environment Runtime is closing")
	}
	if r.deps.beforeSessionConstruction != nil {
		r.deps.beforeSessionConstruction()
	}
	if err := r.initializeSessions(); err != nil {
		r.lifecycleInitErr = err
		return err
	}
	r.mu.Lock()
	r.sessionWorkerDone = make(chan struct{})
	r.mu.Unlock()
	r.startLifecycleEngineLocked()
	r.startIngress(r.config.HistoryIngress)
	go func() {
		r.sessionWorkerErr = r.sessions.Startup(r.lifecycleContext)
		if r.sessionWorkerErr == nil {
			r.mu.Lock()
			r.startupComplete = true
			r.changedLocked()
			r.mu.Unlock()
		}
		close(r.sessionWorkerDone)
		if r.sessionWorkerErr != nil {
			r.fail(fmt.Errorf("Session recovery failed: %w", r.sessionWorkerErr))
		}
	}()
	return nil
}

// startIngress starts Runtime's delivery worker after Session recovery.
func (r *Runtime) startIngress(endpoint configuration.Endpoint) {
	ctx, cancel := context.WithCancel(context.Background())
	worker := &ingressWorker{cancel: cancel, done: make(chan struct{})}
	r.mu.Lock()
	r.ingress = worker
	r.mu.Unlock()
	go func() {
		defer close(worker.done)
		select {
		case <-r.sessionWorkerDone:
		case <-ctx.Done():
			return
		}
		if r.sessionWorkerErr != nil || ctx.Err() != nil {
			return
		}
		reportFailure := func(err error) {
			failure := fmt.Errorf("History ingress stopped: %w", err)
			// Record the cause and revoke admission before delivery cancellation.
			r.fail(failure)

			worker.cancel()
		}
		run := r.deps.runIngress
		if run == nil {
			run = func(ctx context.Context, endpoint configuration.Endpoint) error {
				supervisor, err := r.ingressSupervisor(endpoint, reportFailure)
				if err != nil {
					return err
				}
				return supervisor.Run(ctx)
			}
		}
		if err := run(ctx, endpoint); err != nil && ctx.Err() == nil {
			reportFailure(err)
		}
	}()
}

type ingressWorker struct {
	cancel context.CancelFunc
	done   chan struct{}
}

func (r *Runtime) stopIngress(ctx context.Context) error {
	r.mu.Lock()
	worker := r.ingress
	r.mu.Unlock()
	if worker == nil {
		return nil
	}
	worker.cancel()
	select {
	case <-worker.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runtime) ingressSupervisor(endpoint configuration.Endpoint, onWorkerFailure func(error)) (*ingress.Supervisor, error) {
	config := ingress.SupervisorConfig{
		Target: endpoint.Target, Bearer: endpoint.Bearer, Store: r.store,
		InventoryChanges: r.sessions.InventoryChangesSince, Inventory: r.ingressSources, OnWorkerFailure: onWorkerFailure,
		OnAcknowledgement: func(_ string, _ retained.Acknowledgement) error { r.changed(); return nil },
	}
	if secured := endpoint.SVID; secured != nil {
		transport, err := r.config.Control.Security.ClientCredentials(secured.ServerURI)
		if err != nil {
			return nil, err
		}
		config.Transport, config.ProxyAddress = transport, secured.ProxyAddress
	}
	return ingress.NewSupervisor(config), nil
}

func (r *Runtime) ingressSources() []ingress.Source {
	current := r.sessions.Inventory()
	sources := make([]ingress.Source, 0, len(current))
	for _, s := range current {
		sources = append(sources, ingress.Source{ID: s.ID(), Announce: s.Attached, Journal: s.EventJournal()})
	}
	return sources
}

func (r *Runtime) stopSessions(ctx context.Context) error {
	if r.deps.stopSessions != nil {
		return r.deps.stopSessions(ctx)
	}
	if r.sessions == nil {
		return nil
	}
	select {
	case <-r.sessionWorkerDone:
	case <-ctx.Done():
		return ctx.Err()
	}
	return r.waitSessionStage(ctx, lifecycle.Closed)
}

func (r *Runtime) waitSessionsClosed(ctx context.Context) error {
	if r.deps.waitSessionsClosed != nil {
		return r.deps.waitSessionsClosed(ctx)
	}
	if r.sessions == nil {
		return nil
	}
	return r.waitSessionStage(ctx, lifecycle.Closed)
}

func (r *Runtime) closeSessions(ctx context.Context) error {
	if r.deps.closeSessions != nil {
		return r.deps.closeSessions(ctx)
	}
	if r.sessions == nil {
		return nil
	}
	r.sessionWorkers.Wait()
	return r.sessions.Close()
}

func (r *Runtime) waitForInitialSessions(ctx context.Context) error {
	startupDone := r.sessionWorkerDone
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		if r.closing {
			r.mu.Unlock()
			return errors.New("Runtime closed while starting Session components")
		}
		desired := r.desired
		changed := r.changes
		r.mu.Unlock()
		if r.deps.afterInitialWaitSnapshot != nil {
			r.deps.afterInitialWaitSnapshot()
		}
		if startupDone != nil {
			select {
			case <-startupDone:
				if r.sessionWorkerErr != nil {
					return fmt.Errorf("Session recovery failed: %w", r.sessionWorkerErr)
				}
				startupDone = nil
			default:
			}
		}
		progress, found, err := r.readSessionStatus(ctx)
		if err != nil {
			return err
		}
		if found && progress.Generation == desired.Generation {
			if progress.Error != "" {
				return errors.New(progress.Error)
			}
			if startupDone == nil && progress.NativeStopped {
				r.mu.Lock()
				failure, closing, current := r.failureLocked(), r.closing, r.desired
				r.mu.Unlock()
				if failure != nil {
					return failure
				}
				if closing {
					return errors.New("Runtime closed while starting Session components")
				}
				if current == desired {
					return nil
				}
				continue
			}
		}
		select {
		case <-changed:
		case <-startupDone:
		case <-ctx.Done():
			return ctx.Err()
		case <-r.lifecycleDone:
			return errors.New("Runtime lifecycle engine stopped during Session startup")
		}
	}
}
