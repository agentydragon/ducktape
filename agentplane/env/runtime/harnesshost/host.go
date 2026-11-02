package harnesshost

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"go.uber.org/zap"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Host owns the environment's native harness sessions and their lifecycle.
type Host struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	config              Config
	ctx                 context.Context
	cancel              context.CancelFunc
	supervisor          *supervision.Supervisor
	scripts             *scripts.Runner
	workspaceSupervisor *supervision.Supervisor
	store               *retained.Store
	mu                  sync.Mutex
	sessions            *session.Manager
	createMu            sync.Mutex
	codexAppServer      *codexappserver.Host
	changes             chan struct{}
	fatal               chan error
	idleSince           *timestamppb.Timestamp
	lifecycle           *lifecycle
	closing             bool
	closePublicationErr error
	startupComplete     bool
	startupDone         chan struct{}
	runtimeBinding      atomic.Pointer[runtimeBinding]
	intent              *runtimepb.LifecycleIntent
	bootstrapBinding    *runtimepb.Binding
	ready               atomic.Bool
	nativeStopped       bool
	runtimeBridge       RuntimeBridge
}

// NewEmbedded registers Host controllers in the Runtime-owned engine and store.
// Runtime retains responsibility for stopping and joining that engine.
func NewEmbedded(config Config, lifecycleState state.State, engine *cosiruntime.Runtime, engineDone <-chan struct{}, processes *supervision.Host) (*Host, error) {
	if lifecycleState == nil || engine == nil || engineDone == nil || processes == nil {
		return nil, errors.New("embedded Host requires Runtime state, engine and completion signal")
	}
	if err := config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if _, present := config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return nil, errors.New("CODEX_EXEC_SERVER_URL is owned by execution configuration")
	}
	supervisor := config.ProcessSupervisor
	if supervisor == nil && config.VMProcessLauncher == nil {
		return nil, errors.New("embedded Host requires a Runtime-owned process supervisor")
	}
	var workspaceSupervisor *supervision.Supervisor
	if config.VMProcessLauncher != nil {
		if config.ProcessSupervisor != nil {
			return nil, errors.New("VM Host cannot combine a supplied process supervisor with its systemd launcher")
		}
		var err error
		supervisor, err = config.VMProcessLauncher.PrepareForOwner(config.StateDir, config.Launch.Sandbox.UID, config.VMProcessOwner)
		if err != nil {
			return nil, err
		}
		workspaceSupervisor = supervisor
	}
	store, err := retained.NewStore(filepath.Join(config.StateDir, "sessions"), lifecycleState)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Host{config: config, supervisor: supervisor, workspaceSupervisor: workspaceSupervisor, store: store, ctx: ctx, cancel: cancel, changes: make(chan struct{}), startupDone: make(chan struct{}), fatal: make(chan error, 1)}
	r.scripts, err = scripts.New(scripts.Config{HarnessEnvironment: config.HarnessEnvironment, LaunchProcess: func(ctx context.Context, owner string, cfg supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return r.launchProcess(ctx, owner, r.supervisor, cfg)
	}})
	if err != nil {
		cancel()
		return nil, err
	}
	l, err := newLifecycle(r, lifecycleState, engine, engineDone, processes)
	if err != nil {
		cancel()
		return nil, err
	}
	r.lifecycle = l
	if err := r.configureSessions(); err != nil {
		cancel()
		return nil, err
	}
	if err := l.registerControllers(); err != nil {
		cancel()
		return nil, err
	}
	go l.observeSharedEngine()
	return r, nil
}

func (r *Host) configureSessions() error {
	var err error
	r.sessions, err = session.NewManager(session.Config{
		Context:            r.ctx,
		State:              r.lifecycle.state,
		Store:              r.store,
		WorkspaceRoot:      r.config.WorkspaceRoot,
		Destination:        r.config.Launch.Destination,
		Claude:             r.config.Claude,
		HarnessEnvironment: r.config.HarnessEnvironment,
		DebugCheckpoint:    r.config.DebugCheckpoint,
		Scripts:            r.scripts,
		CodexAppServer:     r.codexAppServer,
		WaitBootstrap:      r.waitBootstrap,
		EnsureWorkspace:    r.ensureWorkspace,
		LaunchProcess: func(ctx context.Context, owner string, cfg supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, owner, r.supervisor, cfg)
		},
		ResolveModelConfig:   r.config.ResolveModelConfig,
		PublishSeal:          r.lifecycle.publishSeal,
		OnChange:             r.changed,
		OnError:              r.fail,
		ReconcileConcurrency: r.config.SessionReconcileConcurrency,
		Environment:          r.environment,
	})
	if err != nil {
		return err
	}
	return nil
}

func (r *Host) changed() {
	r.mu.Lock()
	close(r.changes)
	r.changes = make(chan struct{})
	r.mu.Unlock()
}

func (r *Host) changesSince() <-chan struct{} { r.mu.Lock(); defer r.mu.Unlock(); return r.changes }

func (r *Host) fail(err error) {
	if err == nil {
		return
	}
	for {
		current := r.runtimeBinding.Load()
		if current != nil && current.failed {
			break
		}
		if r.runtimeBinding.CompareAndSwap(current, &runtimeBinding{failed: true}) {
			break
		}
	}
	zap.L().Error("Harness Host operation failed", zap.Error(err))
	select {
	case r.fatal <- err:
	default:
	}
}

func (r *Host) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	config := workspace.Config{Root: r.config.WorkspaceRoot}
	if r.workspaceSupervisor != nil {
		config.LaunchHelper = func(ctx context.Context, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, "workspace-helper", r.workspaceSupervisor, processConfig)
		}
	}
	return workspace.Ensure(ctx, cwd, config)
}

// BeginClose fences local admission and waits for native cleanup. Runtime owns
// the accepted Close operation and calls this after sealed History delivery.
func (r *Host) BeginClose(ctx context.Context) error {
	r.mu.Lock()
	if !r.closing {
		r.closing = true
		r.closePublicationErr = r.lifecycle.setDesiredState(harnesshostlifecycle.Closing, "")
	}
	err := r.closePublicationErr
	r.mu.Unlock()
	r.changed()
	if err != nil {
		return err
	}
	if err := r.WaitClosed(ctx); err != nil {
		return err
	}
	// Controllers stop before joining workers, so no new operation can race Wait.
	r.cancel()
	return nil
}

func (r *Host) isClosing() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.closing
}

// CloseJournals joins the shared engine and Host workers before closing Session
// journals. Runtime first joins ingress, then calls this once during owner release.
func (r *Host) CloseJournals(ctx context.Context) error {
	if err := r.BeginClose(ctx); err != nil {
		return err
	}
	// Runtime stops the shared engine only after every Host controller has
	// published Closed. Join it before closing Host journals.
	<-r.lifecycle.done.Done()
	r.lifecycle.workers.Wait()
	return r.sessions.Close()
}

// WaitClosed observes the Host controller's native cleanup barrier without
// waiting for journal closure, which follows the shared engine join.
func (r *Host) WaitClosed(ctx context.Context) error {
	closed := func() (bool, error) {
		status, found, err := harnesshostlifecycle.ReadStatus(context.Background(), r.lifecycle.state)
		if err != nil || !found {
			return false, err
		}
		if status.Error != "" {
			return false, errors.New(status.Error)
		}
		desired, found, err := harnesshostlifecycle.ReadDesired(context.Background(), r.lifecycle.state)
		if err != nil || !found {
			return false, err
		}
		return status.Stage == harnesshostlifecycle.Closed && status.Generation == desired.Generation, nil
	}
	if done, err := closed(); err != nil || done {
		return err
	}
	err := r.lifecycle.wait(ctx, harnesshostlifecycle.Closed)
	if err == nil {
		return nil
	}
	// Host cancels its own context after publishing Closed. A waiter racing
	// that cancellation must still accept the durable terminal barrier.
	if done, readErr := closed(); readErr == nil && done {
		return nil
	}
	return err
}
