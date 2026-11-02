package harnesshost

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/codexappserver"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/cosi-project/runtime/pkg/controller"

	// Avoid the collision with the Environment Runtime state.
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
)

type lifecycle struct {
	host       *Host
	state      state.State
	engine     *cosiruntime.Runtime
	engineDone <-chan struct{}
	done       context.Context
	finish     context.CancelCauseFunc
	publishMu  sync.Mutex
	workers    sync.WaitGroup
	processes  *supervision.Host
}

func newLifecycle(r *Host, store state.State, engine *cosiruntime.Runtime, engineDone <-chan struct{}, processes *supervision.Host) (*lifecycle, error) {
	if err := r.config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if store == nil || engine == nil || engineDone == nil || processes == nil {
		return nil, errors.New("embedded Host requires the Runtime engine completion signal")
	}
	// Completion is separate from Harness Host cancellation: workers are joined only after the engine exits.
	done, finish := context.WithCancelCause(context.WithoutCancel(r.ctx))
	l := &lifecycle{host: r, state: store, engine: engine, engineDone: engineDone, done: done, finish: finish}
	l.processes = processes.Scope(r.ctx, supervision.HarnessDomain, &l.workers, r.fail)
	if r.config.Codex != nil {
		appServer, err := codexappserver.NewHost(*r.config.Codex, codexappserver.Dependencies{
			Context: r.ctx, State: l.state, Workers: &l.workers, Processes: l.processes,
			Supervisor: r.supervisor, HarnessEnvironment: r.config.HarnessEnvironment,
			StateDir: r.config.StateDir, RequestAdmission: r.admitCodexRequest,
			Admission: r.admitLifecycleEffect, ExecServerURL: r.config.ExecServerURL, ExecutionReady: r.executionReady,
			Readiness: r.config.CodexAppServerReadiness, OnError: r.fail, OnChange: r.changed,
		})
		if err != nil {
			return nil, err
		}
		r.codexAppServer = appServer
		if err := appServer.Initialize(r.ctx); err != nil {
			return nil, err
		}
	}
	if err := harnesshostlifecycle.PublishDesired(l.host.ctx, l.state, harnesshostlifecycle.Desired{Mode: harnesshostlifecycle.Quiescing}); err != nil {
		return nil, err
	}
	if err := l.state.Create(r.ctx, lifecycleResourceFor(harnesshostlifecycle.ResourceID, harnessHostAdmissionCountSpec{}), state.WithCreateOwner(harnesshostlifecycle.HostOwner)); err != nil {
		return nil, err
	}
	return l, nil
}

func (l *lifecycle) observeSharedEngine() {
	<-l.engineDone
	l.finish(errors.New("environment Runtime lifecycle engine stopped"))
}

func (l *lifecycle) registerControllers() error {
	if err := l.engine.RegisterController(&hostIntentController{host: l.host}); err != nil {
		return err
	}
	if err := l.engine.RegisterController(&hostReportController{host: l.host}); err != nil {
		return err
	}
	if err := l.engine.RegisterController(&harnessHostLifecycleController{l: l}); err != nil {
		return err
	}
	var controllers []controller.QController
	if l.host.codexAppServer != nil {
		controllers = append(controllers, l.host.codexAppServer.Controller())
	}
	controllers = append(controllers, l.host.sessions.Controller())
	for _, c := range controllers {
		if err := l.engine.RegisterQController(c); err != nil {
			return err
		}
	}
	return nil
}

// The owning controller has already admitted this operation. Accepted setup and
// bootstrap work still finish if Close has since closed new admissions; their
// operation slots keep shutdown from advancing until they settle.
func (r *Host) launchProcess(ctx context.Context, owner string, supervisor *supervision.Supervisor, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
	generation := r.lifecycle.desiredState().Generation
	return r.lifecycle.processes.Launch(ctx, owner, generation, supervisor, config)
}

func (l *lifecycle) desiredState() harnesshostlifecycle.Desired {
	value, _, err := harnesshostlifecycle.ReadDesired(l.host.ctx, l.state)
	if err != nil {
		l.host.fail(err)
		return harnesshostlifecycle.Desired{Mode: harnesshostlifecycle.Closing}
	}
	return value
}

// Caller holds the Harness Host admission lock.
func (l *lifecycle) setDesiredState(mode harnesshostlifecycle.Mode, sealUID string) error {
	desiredState := l.desiredState()
	if desiredState.Mode == harnesshostlifecycle.Closing && mode != harnesshostlifecycle.Closing {
		return errors.New("Harness Host is closing")
	}
	retry := l.status().Error != ""
	if mode == harnesshostlifecycle.Running && desiredState.Mode != harnesshostlifecycle.Running {
		// Drop old execution requests while admission is still closed. Recovery
		// publishes fresh demand after eligible journals have reopened.
		if err := l.host.sessions.ResetDemand(); err != nil {
			return err
		}
		if l.host.codexAppServer != nil {
			if err := l.host.codexAppServer.ResetDemand(l.host.ctx); err != nil {
				return err
			}
		}

	}
	if desiredState.Mode != mode || desiredState.SealUID != sealUID || retry {
		desiredState.Mode, desiredState.SealUID, desiredState.Generation = mode, sealUID, desiredState.Generation+1
	}
	return harnesshostlifecycle.PublishDesired(l.host.ctx, l.state, desiredState)
}

func (r *Host) admitLifecycleEffect(ctx context.Context, expected harnesshostlifecycle.Desired, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || !r.ready.Load() || r.intent == nil || r.intent.Binding == nil ||
		r.intent.Mode != runtimepb.LifecycleIntent_RUNNING || r.intent.Binding.Generation != expected.Generation ||
		!r.RuntimeBindingMatches(r.intent.Binding.RuntimeBootId) {
		return nil
	}
	current, found, err := harnesshostlifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil {
		return err
	}
	if !found || current != expected || current.Mode != harnesshostlifecycle.Running {
		return nil
	}
	return effect(ctx)
}

func (r *Host) admitCodexRequest(ctx context.Context, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.lifecycle.accepting() {
		return errors.New("Harness Host is not accepting new Codex work")
	}

	return effect(ctx)
}

func (l *lifecycle) accepting() bool {
	return l.desiredState().Mode == harnesshostlifecycle.Running && l.host.ready.Load()
}

func (l *lifecycle) status() harnesshostlifecycle.Status {
	value, _, err := harnesshostlifecycle.ReadStatus(l.host.ctx, l.state)
	if err != nil {
		l.host.fail(err)
	}
	return value
}

func (l *lifecycle) wait(ctx context.Context, minimum harnesshostlifecycle.Stage) error {
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	stop := context.AfterFunc(l.host.ctx, func() { cancel(context.Cause(l.host.ctx)) })
	defer stop()
	stopEngine := context.AfterFunc(l.done, func() { cancel(context.Cause(l.done)) })
	defer stopEngine()
	err := harnesshostlifecycle.WatchStatus(ctx, l.state, func(current harnesshostlifecycle.Status) (bool, error) {
		if current.Error != "" {
			return false, errors.New(current.Error)
		}
		return current.Generation == l.desiredState().Generation && current.Stage >= minimum, nil
	})
	if err != nil && errors.Is(err, ctx.Err()) {
		return context.Cause(ctx)
	}
	return err
}
