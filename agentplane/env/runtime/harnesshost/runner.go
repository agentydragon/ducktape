package harnesshost

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/workspace"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Runner struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	config              Config
	ctx                 context.Context
	cancel              context.CancelFunc
	owner               *retained.Owner
	supervisor          *supervision.Supervisor
	scripts             *scripts.Runner
	workspaceSupervisor *supervision.Supervisor
	store               *retained.Store
	bootID              string
	mu                  sync.Mutex
	sessions            *session.Manager
	createMu            sync.Mutex
	codexAppServer      *codexappserver.Host
	changes             chan struct{}
	fatal               chan error
	idleSince           *timestamppb.Timestamp
	lifecycle           *lifecycle
	closeOnce           sync.Once
	closeDone           chan struct{}
	closeErr            error
	closing             bool
	startupComplete     bool
	runtimeConnection   atomic.Pointer[runtimeConnection]
	intent              *runtimepb.LifecycleIntent
	bootstrapBinding    *runtimepb.Binding
	ready               atomic.Bool
	nativeStopped       bool
	runtimeBridge       RuntimeBridge
}

// NewEmbedded registers Host controllers in the Runtime-owned engine and store.
// Runtime retains responsibility for stopping and joining that engine.
func NewEmbedded(config Config, lifecycleState state.State, engine *cosiruntime.Runtime, engineDone <-chan struct{}, processes *supervision.Host) (*Runner, error) {
	if lifecycleState == nil || engine == nil || engineDone == nil || processes == nil {
		return nil, errors.New("embedded Host requires Runtime state, engine and completion signal")
	}
	if err := config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if _, present := config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return nil, errors.New("CODEX_EXEC_SERVER_URL is owned by execution configuration")
	}
	// The runtime retains the legacy environment lock. This private lock fences only
	// native state ownership; retained Session paths and journal identities stay put.
	owner, err := retained.OpenOwner(filepath.Join(config.StateDir, "harness-host"))
	if err != nil {
		return nil, err
	}
	supervisor := config.ProcessSupervisor
	if supervisor == nil && config.VMProcessLauncher == nil {
		supervisor = supervision.NewSubprocessSupervisor(owner.File())
	}
	var workspaceSupervisor *supervision.Supervisor
	if config.VMProcessLauncher != nil {
		if config.ProcessSupervisor != nil {
			_ = owner.Close()
			return nil, errors.New("VM Host cannot combine a supplied process supervisor with its systemd launcher")
		}
		supervisor, err = config.VMProcessLauncher.PrepareForOwner(config.StateDir, config.Launch.Sandbox.UID, config.VMProcessOwner)
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
		workspaceSupervisor = supervisor
	}
	store, err := retained.NewStore(filepath.Join(config.StateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Runner{config: config, owner: owner, supervisor: supervisor, workspaceSupervisor: workspaceSupervisor, store: store, bootID: uuid.NewString(), ctx: ctx, cancel: cancel, changes: make(chan struct{}), fatal: make(chan error, 1)}
	r.scripts, err = scripts.New(scripts.Config{HarnessEnvironment: config.HarnessEnvironment, LaunchProcess: func(ctx context.Context, owner string, cfg supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return r.launchProcess(ctx, owner, r.supervisor, cfg)
	}})
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	l, err := newLifecycle(r, lifecycleState, engine, engineDone, processes)
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	r.lifecycle = l
	if err := r.configureSessions(); err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	if err := l.registerControllers(); err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	go l.observeSharedEngine()
	return r, nil
}

func (r *Runner) configureSessions() error {
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
		PublishAck:           r.lifecycle.publishAck,
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

func (r *Runner) changed() {
	r.mu.Lock()
	close(r.changes)
	r.changes = make(chan struct{})
	r.mu.Unlock()
}

func (r *Runner) changesSince() <-chan struct{} { r.mu.Lock(); defer r.mu.Unlock(); return r.changes }

func (r *Runner) fail(err error) {
	if err == nil {
		return
	}
	r.runtimeConnection.Store(nil)
	zap.L().Error("runner operation failed", zap.Error(err))
	select {
	case r.fatal <- err:
	default:
	}
}

func (r *Runner) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	config := workspace.Config{Root: r.config.WorkspaceRoot}
	if r.workspaceSupervisor != nil {
		config.LaunchHelper = func(ctx context.Context, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, "workspace-helper", r.workspaceSupervisor, processConfig)
		}
	}
	return workspace.Ensure(ctx, cwd, config)
}

// Close starts local process cleanup once. Canceling ctx ends only this caller's wait;
// accepted setup/bootstrap and the shared cleanup continue under Runner ownership.
func (r *Runner) Close(ctx context.Context) error {
	r.closeOnce.Do(func() {
		r.mu.Lock()
		r.closing = true
		r.mu.Unlock()
		r.closeDone = make(chan struct{})
		go func() {
			r.closeErr = r.finishClose()
			close(r.closeDone)
		}()
	})
	select {
	case <-r.closeDone:
		return r.closeErr
	default:
	}
	select {
	case <-r.closeDone:
		return r.closeErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runner) isClosing() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.closing
}

func (r *Runner) finishClose() error {
	r.mu.Lock()
	err := r.lifecycle.setDesiredState(runnerlifecycle.Closing, "")
	r.mu.Unlock()
	r.changed()
	if err == nil {
		err = r.WaitClosed(r.ctx)
	}
	if err != nil {
		return err
	}
	// Controllers stop before joining workers, so no new operation can race Wait.
	r.cancel()
	// Runtime stops the shared engine only after every Host controller has
	// published Closed. Join it before releasing Host-owned native state.
	<-r.lifecycle.done.Done()
	r.lifecycle.workers.Wait()
	errs := []error{r.sessions.Close()}

	if err := errors.Join(errs...); err != nil {
		return err
	}
	return r.owner.Close()
}

// WaitClosed observes the Host controller's native cleanup barrier without
// waiting for its final owner release, which follows the shared engine join.
func (r *Runner) WaitClosed(ctx context.Context) error {
	closed := func() (bool, error) {
		status, found, err := runnerlifecycle.ReadStatus(context.Background(), r.lifecycle.state)
		if err != nil || !found {
			return false, err
		}
		if status.Error != "" {
			return false, errors.New(status.Error)
		}
		desired, found, err := runnerlifecycle.ReadDesired(context.Background(), r.lifecycle.state)
		if err != nil || !found {
			return false, err
		}
		return status.Stage == runnerlifecycle.Closed && status.Generation == desired.Generation, nil
	}
	if done, err := closed(); err != nil || done {
		return err
	}
	err := r.lifecycle.wait(ctx, runnerlifecycle.Closed)
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
