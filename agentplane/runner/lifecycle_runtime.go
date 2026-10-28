package runner

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/codex/execserver"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"

	// Avoid the collision with the runner runtime state.
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

type lifecycle struct {
	runner    *Runner
	state     state.State
	engine    *cosiruntime.Runtime
	done      chan struct{}
	publishMu sync.Mutex
	workers   sync.WaitGroup
	processes *supervision.Host
}

func newLifecycle(r *Runner, adapters *execserver.Adapters) (*lifecycle, error) {
	if err := r.config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.L().Named("lifecycle"))
	if err != nil {
		return nil, err
	}
	l := &lifecycle{runner: r, state: store, engine: engine, done: make(chan struct{})}
	l.processes = supervision.NewHost(r.ctx, l.state, runnerlifecycle.Namespace, &l.workers, r.fail)
	bootstrapHost, err := bootstrap.NewHost(bootstrap.Config{
		Script: r.config.Launch.Bootstrap, StateDir: r.config.StateDir,
		WorkingDirectory: r.config.InitializationCwd,
	}, bootstrap.Dependencies{
		Context: r.ctx, State: l.state, Workers: &l.workers, Scripts: r.scripts,
		Admission: r.admitLifecycleEffect, OnError: r.fail, OnRuntimeChange: r.changed,
	})
	if err != nil {
		return nil, err
	}
	r.bootstrapHost = bootstrapHost
	if err := bootstrapHost.Initialize(r.ctx); err != nil {
		return nil, err
	}
	if r.config.CodexExecServer != nil {
		if adapters == nil {
			return nil, errors.New("Codex exec-server adapters are required")
		}
		executor, err := execserver.NewHost(*r.config.CodexExecServer, execserver.Dependencies{
			Context: r.ctx, State: l.state, Workers: &l.workers, Processes: l.processes,
			Admission: r.admitLifecycleEffect, OnError: r.fail, OnRuntimeChange: r.changed,
		}, *adapters)
		if err != nil {
			return nil, err
		}
		r.codexExecServer = executor
		if err := executor.Initialize(r.ctx); err != nil {
			return nil, err
		}
	}
	if r.config.Codex != nil {
		appServer, err := codexappserver.NewHost(*r.config.Codex, codexappserver.Dependencies{
			Context: r.ctx, State: l.state, Workers: &l.workers, Processes: l.processes,
			Supervisor: r.supervisor, HarnessEnvironment: r.config.HarnessEnvironment,
			StateDir: r.config.StateDir, RequestAdmission: r.admitCodexRequest,
			Admission: r.admitLifecycleEffect, ExecServer: r.codexExecServer, OnError: r.fail, OnChange: r.changed,
		})
		if err != nil {
			return nil, err
		}
		r.codexAppServer = appServer
		if err := appServer.Initialize(r.ctx); err != nil {
			return nil, err
		}
	}
	if err := engine.RegisterController(&runnerLifecycleController{l: l}); err != nil {
		return nil, err
	}
	if err := engine.RegisterQController(&sessionLifecycleController{l: l}); err != nil {
		return nil, err
	}
	controllers := []controller.QController{l.processes.Controller()}
	if r.codexExecServer != nil {
		controllers = append(controllers, r.codexExecServer.Controller())
	}
	if r.codexAppServer != nil {
		controllers = append(controllers, r.codexAppServer.Controller())
	}
	controllers = append(controllers, bootstrapHost.Controller())
	for _, c := range controllers {
		if err := engine.RegisterQController(c); err != nil {
			return nil, err
		}
	}
	if err := runnerlifecycle.PublishDesired(l.runner.ctx, l.state, runnerlifecycle.Desired{}); err != nil {
		return nil, err
	}
	return l, nil
}

// The owning controller has already admitted this operation. Accepted setup and
// bootstrap work still finish if Close has since closed new admissions; their
// operation slots keep shutdown from advancing until they settle.
func (r *Runner) launchProcess(ctx context.Context, owner string, supervisor *supervision.Supervisor, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
	generation := r.lifecycle.desiredState().Generation
	return r.lifecycle.processes.Launch(ctx, owner, generation, supervisor, config)
}

func (l *lifecycle) run() {
	defer close(l.done)
	if err := l.engine.Run(l.runner.ctx); err != nil && !errors.Is(err, context.Canceled) {
		l.runner.fail(err)
	}
}

func (l *lifecycle) desiredState() runnerlifecycle.Desired {
	value, _, err := runnerlifecycle.ReadDesired(l.runner.ctx, l.state)
	if err != nil {
		l.runner.fail(err)
		return runnerlifecycle.Desired{Mode: runnerlifecycle.Closing}
	}
	return value
}

// Caller holds Runner.mu, the synchronous admission barrier.
func (l *lifecycle) setDesiredState(mode runnerlifecycle.Mode, sealUID string) error {
	desiredState := l.desiredState()
	if desiredState.Mode == runnerlifecycle.Closing && mode != runnerlifecycle.Closing {
		return errors.New("runner is closing")
	}
	if mode == runnerlifecycle.Closing && sealUID == "" {
		sealUID = desiredState.SealUID
	}
	retry := l.status().Error != ""
	if mode == runnerlifecycle.Running && desiredState.Mode != runnerlifecycle.Running {
		// Drop old execution requests while admission is still closed. Recovery
		// publishes fresh demand after eligible journals have reopened.
		l.publishMu.Lock()
		for _, session := range l.runner.sessions.inventory() {
			id := session.id
			input, _, err := lifecycleRead[sessionInputSpec](l.runner.ctx, l.state, id)
			if err != nil {
				l.publishMu.Unlock()
				return err
			}
			input.Demand = 0
			if err := publishLifecycle(l, id, input); err != nil {
				l.publishMu.Unlock()
				return err
			}
		}
		if l.runner.codexAppServer != nil {
			if err := l.runner.codexAppServer.ResetDemand(l.runner.ctx); err != nil {
				l.publishMu.Unlock()
				return err
			}
		}
		l.publishMu.Unlock()
		if l.runner.codexExecServer != nil {
			if err := l.runner.codexExecServer.ResetDemand(l.runner.ctx); err != nil {
				return err
			}
		}
	}
	if desiredState.Mode != mode || desiredState.SealUID != sealUID || retry {
		desiredState.Mode, desiredState.SealUID, desiredState.Generation = mode, sealUID, desiredState.Generation+1
	}
	return runnerlifecycle.PublishDesired(l.runner.ctx, l.state, desiredState)
}

func (r *Runner) admitLifecycleEffect(ctx context.Context, expected runnerlifecycle.Desired, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	current, found, err := runnerlifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil {
		return err
	}
	if !found || current != expected || current.Mode != runnerlifecycle.Running {
		return nil
	}
	return effect(ctx)
}

func (r *Runner) admitCodexRequest(ctx context.Context, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.lifecycle.accepting() {
		return errors.New("runner is not accepting new Codex work")
	}
	if r.codexExecServer != nil {
		if err := r.codexExecServer.RequestLocked(ctx); err != nil {
			return err
		}
	}
	return effect(ctx)
}

func (l *lifecycle) accepting() bool {
	return l.desiredState().Mode == runnerlifecycle.Running && !l.runner.store.Lameduck()
}

func (l *lifecycle) status() runnerlifecycle.Status {
	value, _, err := runnerlifecycle.ReadStatus(l.runner.ctx, l.state)
	if err != nil {
		l.runner.fail(err)
	}
	return value
}

func (l *lifecycle) wait(ctx context.Context, minimum runnerlifecycle.Stage) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(l.runner.ctx, cancel)
	defer stop()
	return runnerlifecycle.WatchStatus(ctx, l.state, func(current runnerlifecycle.Status) (bool, error) {
		if current.Error != "" {
			return false, errors.New(current.Error)
		}
		return current.Generation == l.desiredState().Generation && current.Stage >= minimum, nil
	})
}
