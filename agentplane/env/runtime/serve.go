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
	"google.golang.org/protobuf/proto"
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
	if r.config.CodexExecServer != nil {
		config.ExecServerURL = r.config.CodexExecServer.URL
	}
	if r.config.Supervision == "systemd" {
		config.VMProcessOwner = supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: HostScopeID,
		}
	} else if r.config.Supervision == "pid1" {
		// One PID1 reaper owns all child waits in the container. A subprocess
		// supervisor would race it for native and workspace-helper exits.
		config.ProcessSupervisor = r.supervisor
	}
	if r.deps.beforeHostConstruction != nil {
		r.deps.beforeHostConstruction()
	}
	host, err := harnesshost.NewEmbedded(config, r.lifecycleState, r.lifecycleEngine, r.lifecycleDone)
	if err != nil {
		r.lifecycleInitErr = err
		return err
	}
	hostCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	process := &embeddedHostLifetime{host: host, cancel: cancel, done: make(chan struct{})}
	r.mu.Lock()
	r.hostLifetime = process
	r.hostExit = make(chan childExit, 1)
	r.mu.Unlock()
	r.startLifecycleEngineLocked()
	go func() {
		process.err = host.Run(hostCtx, r.config.HostIngress, r)
		close(process.done)
		code, err := process.Wait(context.Background())
		exit := childExit{code: code, err: err}
		r.mu.Lock()
		exitChannel := r.hostExit
		closing := r.closing
		r.hostExited = true
		r.mu.Unlock()
		if exitChannel != nil {
			exitChannel <- exit
		}
		if !closing {
			if err != nil {
				r.fail(fmt.Errorf("Harness Host stopped before Runtime shutdown (status=%d): %w", code, err))
			} else {
				r.fail(fmt.Errorf("Harness Host stopped before Runtime shutdown (status=%d)", code))
			}
		}
	}()
	return nil
}

// embeddedHostLifetime keeps Runtime's existing stop-and-join fence while Host
// runs in the same management process. Its context is independent of the
// Runtime Run caller, so a signal cannot bypass the coordinated drain.
type embeddedHostLifetime struct {
	host   *harnesshost.Runner
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func (p *embeddedHostLifetime) Stop(ctx context.Context) error {
	p.cancel()
	return ctx.Err()
}

func (p *embeddedHostLifetime) WaitClosed(ctx context.Context) error { return p.host.WaitClosed(ctx) }

func (p *embeddedHostLifetime) Cleanup(ctx context.Context) error { return p.host.Close(ctx) }

func (p *embeddedHostLifetime) Wait(ctx context.Context) (int, error) {
	select {
	case <-p.done:
		if p.err != nil {
			return 1, p.err
		}
		return 0, nil
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (r *Runtime) waitForInitialHost(ctx context.Context) error {
	for {
		r.mu.Lock()
		if r.failed {
			r.mu.Unlock()
			return errors.New("Runtime failed while starting Harness Host")
		}
		if r.host != nil && r.host.connected && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE &&
			r.host.report != nil && r.host.report.Ready && r.host.report.NativeStopped &&
			protoEqualBindings(r.host.report.Binding, r.intent.Binding) {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		exitChannel := r.hostExit
		r.mu.Unlock()
		select {
		case <-changed:
		case exit := <-exitChannel:
			return fmt.Errorf("Harness Host exited before initial lifecycle acknowledgement (status=%d): %w", exit.code, exit.err)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func protoEqualBindings(left, right *runtimepb.Binding) bool {
	return proto.Equal(left, right)
}
