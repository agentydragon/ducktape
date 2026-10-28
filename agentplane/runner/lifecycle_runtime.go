package runner

import (
	"context"
	"errors"
	"sync"

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
}

func newLifecycle(r *Runner) (*lifecycle, error) {
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := cosiruntime.NewRuntime(store, zap.L().Named("lifecycle"))
	if err != nil {
		return nil, err
	}
	l := &lifecycle{runner: r, state: store, engine: engine, done: make(chan struct{})}
	if err := engine.RegisterController(&runnerLifecycleController{l: l}); err != nil {
		return nil, err
	}
	if err := engine.RegisterQController(&sessionLifecycleController{l: l}); err != nil {
		return nil, err
	}
	for _, c := range []controller.QController{
		&executorLifecycleController{l: l, host: r.executor},
		&codexLifecycleController{l: l, host: r.codex, executor: r.executor},
		&bootstrapLifecycleController{l: l},
	} {
		if err := engine.RegisterQController(c); err != nil {
			return nil, err
		}
	}
	if err := publishLifecycle(l, "executor", executorInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, "codex", codexInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, "bootstrap", bootstrapInputSpec{}); err != nil {
		return nil, err
	}

	if err := publishLifecycle(l, "runner", runnerGoalSpec{}); err != nil {
		return nil, err
	}
	return l, nil
}

func (l *lifecycle) run() {
	defer close(l.done)
	if err := l.engine.Run(l.runner.ctx); err != nil && !errors.Is(err, context.Canceled) {
		l.runner.fail(err)
	}
}

func (l *lifecycle) goal() runnerGoalSpec {
	value, _, err := lifecycleRead[runnerGoalSpec](l.runner.ctx, l.state, "runner")
	if err != nil {
		l.runner.fail(err)
		return runnerGoalSpec{Mode: runnerClosing}
	}
	return value
}

// Caller holds Runner.mu, the synchronous admission barrier.
func (l *lifecycle) setGoal(mode runnerMode, sealUID string) error {
	goal := l.goal()
	if goal.Mode == runnerClosing && mode != runnerClosing {
		return errors.New("runner is closing")
	}
	if mode == runnerClosing && sealUID == "" {
		sealUID = goal.SealUID
	}
	retry := l.status().Error != ""
	if mode == runnerRunning && goal.Mode != runnerRunning {
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
		if err := publishLifecycle(l, "codex", codexInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		if err := publishLifecycle(l, "executor", executorInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		l.publishMu.Unlock()
	}
	if goal.Mode != mode || goal.SealUID != sealUID || retry {
		goal.Mode, goal.SealUID, goal.Generation = mode, sealUID, goal.Generation+1
	}
	return publishLifecycle(l, "runner", goal)
}

func (l *lifecycle) accepting() bool {
	return l.goal().Mode == runnerRunning && !l.runner.store.Lameduck()
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
		return current.Generation == l.goal().Generation && current.Stage >= minimum, nil
	})
}
