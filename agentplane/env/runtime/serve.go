package runtime

import (
	"context"
	"errors"
	"fmt"
	"time"

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
	hostCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	r.mu.Lock()
	r.sessions = host
	r.sessionWorkerCancel = cancel
	r.sessionWorkerDone = make(chan struct{})
	r.mu.Unlock()
	bindErr := r.bindHost()
	r.startLifecycleEngineLocked()
	go func() {
		r.sessionWorkerErr = host.Run(hostCtx, r.config.HostIngress, r)
		close(r.sessionWorkerDone)
		r.mu.Lock()
		closing := r.closing
		r.mu.Unlock()
		if !closing {
			if r.sessionWorkerErr != nil {
				r.fail(fmt.Errorf("Session worker stopped before Runtime shutdown: %w", r.sessionWorkerErr))
			} else {
				r.fail(errors.New("Session worker stopped before Runtime shutdown"))
			}
		}
	}()
	return bindErr
}

func (r *Runtime) stopSessions(ctx context.Context) error {
	r.sessionWorkerCancel()
	if err := ctx.Err(); err != nil {
		return err
	}
	if r.deps.stopSessions != nil {
		return r.deps.stopSessions(ctx)
	}
	return nil
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
	return r.sessions.Close(ctx)
}

func (r *Runtime) waitForInitialHost(ctx context.Context) error {
	for {
		r.mu.Lock()
		if r.failed || r.closing {
			r.mu.Unlock()
			return errors.New("Runtime failed or closed while starting Harness Host")
		}
		report := r.hostReportLocked()
		ready := r.host != nil && r.host.active && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE &&
			report != nil && report.Ready && report.NativeStopped
		changed := r.changes
		workerDone := r.sessionWorkerDone
		r.mu.Unlock()
		if r.deps.afterInitialWaitSnapshot != nil {
			r.deps.afterInitialWaitSnapshot()
		}
		// A completed Host may leave an already-published ready report behind.
		// The lifetime exit takes precedence before exposing Gateway control.
		select {
		case <-workerDone:
			return r.initialSessionWorkerExit()
		default:
		}
		if ready {
			return nil
		}
		select {
		case <-changed:
		case <-workerDone:
			return r.initialSessionWorkerExit()
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (r *Runtime) initialSessionWorkerExit() error {
	if r.sessionWorkerErr != nil {
		return fmt.Errorf("Session worker stopped before initial lifecycle acknowledgement: %w", r.sessionWorkerErr)
	}
	return errors.New("Session worker stopped before initial lifecycle acknowledgement")
}
