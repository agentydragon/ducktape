package runner

import (
	"context"
	"errors"
	"sync"

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

func newLifecycle(r *Runner) (*lifecycle, error) {
	if err := r.config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.L().Named("lifecycle"))
	if err != nil {
		return nil, err
	}
	l := &lifecycle{runner: r, state: store, engine: engine, done: make(chan struct{})}
	l.processes = supervision.NewHost(r.ctx, l.state, lifecycleNamespace, &l.workers, r.fail)
	if err := engine.RegisterController(&runnerLifecycleController{l: l}); err != nil {
		return nil, err
	}
	if err := engine.RegisterQController(&sessionLifecycleController{l: l}); err != nil {
		return nil, err
	}
	for _, c := range []controller.QController{
		l.processes.Controller(),
		&codexExecServerLifecycleController{l: l, host: r.codexExecServer},
		&codexAppServerLifecycleController{l: l, host: r.codexAppServer, codexExecServer: r.codexExecServer},
		&bootstrapLifecycleController{l: l},
	} {
		if err := engine.RegisterQController(c); err != nil {
			return nil, err
		}
	}
	if err := publishLifecycle(l, codexExecServerResourceID, codexExecServerInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, codexAppServerResourceID, codexAppServerInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, "bootstrap", bootstrapInputSpec{}); err != nil {
		return nil, err
	}

	if err := publishLifecycle(l, "runner", runnerDesiredStateSpec{}); err != nil {
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

func (l *lifecycle) desiredState() runnerDesiredStateSpec {
	value, _, err := lifecycleRead[runnerDesiredStateSpec](l.runner.ctx, l.state, "runner")
	if err != nil {
		l.runner.fail(err)
		return runnerDesiredStateSpec{Mode: runnerClosing}
	}
	return value
}

// Caller holds Runner.mu, the synchronous admission barrier.
func (l *lifecycle) setDesiredState(mode runnerMode, sealUID string) error {
	desiredState := l.desiredState()
	if desiredState.Mode == runnerClosing && mode != runnerClosing {
		return errors.New("runner is closing")
	}
	if mode == runnerClosing && sealUID == "" {
		sealUID = desiredState.SealUID
	}
	retry := l.status().Error != ""
	if mode == runnerRunning && desiredState.Mode != runnerRunning {
		// Drop old execution requests while admission is still closed. Recovery
		// publishes fresh demand after eligible journals have reopened.
		l.publishMu.Lock()
		for id := range l.runner.sessions {
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
		if err := publishLifecycle(l, codexAppServerResourceID, codexAppServerInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		if err := publishLifecycle(l, codexExecServerResourceID, codexExecServerInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		l.publishMu.Unlock()
	}
	if desiredState.Mode != mode || desiredState.SealUID != sealUID || retry {
		desiredState.Mode, desiredState.SealUID, desiredState.Generation = mode, sealUID, desiredState.Generation+1
	}
	return publishLifecycle(l, "runner", desiredState)
}

func (l *lifecycle) accepting() bool {
	return l.desiredState().Mode == runnerRunning && !l.runner.store.Lameduck()
}

func (l *lifecycle) status() runnerRuntimeSpec {
	value, _, err := lifecycleRead[runnerRuntimeSpec](l.runner.ctx, l.state, "runner")
	if err != nil {
		l.runner.fail(err)
	}
	return value
}

func (l *lifecycle) wait(ctx context.Context, minimum runnerStage) error {
	return watchLifecycle(ctx, l, "runner", func(current runnerRuntimeSpec) (bool, error) {
		if current.Error != "" {
			return false, errors.New(current.Error)
		}
		return current.Generation == l.desiredState().Generation && current.Stage >= minimum, nil
	})
}
