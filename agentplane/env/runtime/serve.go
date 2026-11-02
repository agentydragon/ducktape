package runtime

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"google.golang.org/grpc"
)

const hostStartupTimeout = 90 * time.Second

// Run starts the embedded Host coordinator and Gateway-facing control server.
// Runtime owns the environment lifecycle and management-process lifetime.
func (r *Runtime) Run(ctx context.Context) error {
	if err := r.startHost(ctx); err != nil {
		r.lifecycleStartMu.Lock()
		unsafe := r.lifecycleInitErr != nil
		r.lifecycleStartMu.Unlock()
		if unsafe {
			// No Host native work has started. The partial controller set cannot
			// be run safely; retain the environment owner for process-level fencing.
			return fmt.Errorf("start Harness Host: %w", err)
		}
		return errors.Join(fmt.Errorf("start Harness Host: %w", err), r.Close(context.Background()))
	}
	startupCtx, cancelStartup := context.WithTimeout(ctx, hostStartupTimeout)
	err := r.waitForInitialHost(startupCtx)
	cancelStartup()
	if err != nil {
		return errors.Join(fmt.Errorf("wait for initial Harness Host drain: %w", err), r.Close(context.Background()))
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

func (r *Runtime) startHost(ctx context.Context) error {
	r.lifecycleStartMu.Lock()
	defer r.lifecycleStartMu.Unlock()
	r.mu.Lock()
	closing := r.closing
	r.mu.Unlock()
	if closing {
		return errors.New("environment Runtime is closing")
	}
	config := r.config.HostConfig
	config.StateDir = r.config.StateDir
	if r.config.CodexExecServer != nil {
		config.ExecServerURL = r.config.CodexExecServer.URL
	}
	if r.config.Supervision == "systemd" {
		config.VMProcessOwner = supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: HostScopeID,
		}
	} else {
		// Native harnesses inherit the Runtime owner descriptor in both Pod modes.
		// PID1 also requires one shared reaper for every child wait.
		config.ProcessSupervisor = r.supervisor
	}
	if r.deps.beforeHostConstruction != nil {
		r.deps.beforeHostConstruction()
	}
	host, err := harnesshost.NewEmbedded(config, r.lifecycleState, r.lifecycleEngine, r.lifecycleDone, r.processes)
	if err != nil {
		r.lifecycleInitErr = err
		return err
	}
	r.mu.Lock()
	r.sessions = host
	r.sessionWorkerDone = make(chan struct{})
	r.mu.Unlock()
	bindErr := r.bindHost()
	r.startLifecycleEngineLocked()
	r.startIngress(host, r.config.HostIngress)
	r.lifecycleWorkers.Add(1)
	go func() {
		defer r.lifecycleWorkers.Done()
		select {
		case err := <-host.Failures():
			r.HostFailed(err)
		case <-r.lifecycleDone:
		}
	}()
	go func() {
		r.sessionWorkerErr = host.Startup(r)
		close(r.sessionWorkerDone)
		if r.sessionWorkerErr != nil {
			r.fail(fmt.Errorf("Session recovery failed: %w", r.sessionWorkerErr))
		}
	}()
	return bindErr
}

// startIngress starts Runtime's delivery worker after Session recovery.
func (r *Runtime) startIngress(host *harnesshost.Host, endpoint configuration.Endpoint) {
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
		run := r.deps.runIngress
		if run == nil {
			run = func(ctx context.Context, host *harnesshost.Host, endpoint configuration.Endpoint) error {
				return host.IngressSupervisor(endpoint).Run(ctx)
			}
		}
		if err := run(ctx, host, endpoint); err != nil && ctx.Err() == nil {
			failure := fmt.Errorf("History ingress stopped: %w", err)
			host.ReportFailure(failure)
			r.fail(failure)
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
	return r.sessions.BeginClose(ctx)
}

func (r *Runtime) waitSessionsClosed(ctx context.Context) error {
	if r.deps.waitSessionsClosed != nil {
		return r.deps.waitSessionsClosed(ctx)
	}
	return r.sessions.WaitClosed(ctx)
}

func (r *Runtime) closeSessions(ctx context.Context) error {
	if r.deps.closeSessions != nil {
		return r.deps.closeSessions(ctx)
	}
	return r.sessions.CloseJournals(ctx)
}

func (r *Runtime) waitForInitialHost(ctx context.Context) error {
	startupDone := r.sessionWorkerDone
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		if r.closing {
			r.mu.Unlock()
			return errors.New("Runtime closed while starting Session component")
		}
		report := r.hostReportLocked()
		ready := r.host != nil && r.host.active && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE &&
			report != nil && report.Ready && report.NativeStopped
		changed := r.changes
		r.mu.Unlock()
		if r.deps.afterInitialWaitSnapshot != nil {
			r.deps.afterInitialWaitSnapshot()
		}
		if startupDone != nil {
			select {
			case <-startupDone:
				if r.sessionWorkerErr != nil {
					return fmt.Errorf("Session recovery failed before initial lifecycle acknowledgement: %w", r.sessionWorkerErr)
				}
				startupDone = nil
			default:
			}
		}
		if ready && startupDone == nil {
			return nil
		}
		select {
		case <-changed:
		case <-startupDone:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}
