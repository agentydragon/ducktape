// Package runtime owns one Sandbox environment's identity, bootstrap and component lifecycles.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	RuntimeUnitName                            = "agentplane-environment-runtime.service"
	RuntimeScopeID  supervision.SystemdScopeID = supervision.SystemdEnvironmentRuntimeScopeID
	HarnessScopeID  supervision.SystemdScopeID = "harness"
)

// Config contains the resolved environment configuration.
type Config struct {
	Control                     configuration.EnvironmentRuntimeControlConfiguration
	StateDir                    string
	Launch                      configuration.SandboxLaunch
	Supervision                 string
	PodPID1                     bool
	VMProcessLauncher           *supervision.SystemdLauncher
	CodexExecServer             *codexexecserver.Config
	CodexBinary                 string
	HarnessEnvironment          map[string]string
	InitializationCwd           string
	SessionReconcileConcurrency int
	Claude, Codex               *harness.LaunchConfig
	CodexAppServerReadiness     codexappserver.Readiness
	WorkspaceRoot               string
	DebugCheckpoint             *session.DebugCheckpointConfig
	HTTPClient                  *http.Client
	HistoryIngress              configuration.Endpoint
}

type childExit struct {
	code int
	err  error
}

type runtimeDeps struct {
	prepareSupervisor         func(Config, *os.File) (*supervision.Supervisor, error)
	waitBootstrap             func(context.Context) error
	startExecution            func(context.Context, Config, string) (executionHandle, error)
	beforeSessionConstruction func()
	afterInitialWaitSnapshot  func()
	stopSessions              func(context.Context) error
	waitSessionsClosed        func(context.Context) error
	closeSessions             func(context.Context) error
	runIngress                func(context.Context, configuration.Endpoint) error
}

type executionHandle interface {
	Executor() executorpb.ExecutorServer
	StopAndFence(context.Context) error
}

// Runtime owns environment lifecycle and its concrete components.
type Runtime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	config Config
	deps   runtimeDeps

	mu                     sync.Mutex
	transitionMu           sync.Mutex
	transitionUnsafe       error
	activeTransitionCancel context.CancelFunc

	owner                  *environmentOwner
	state                  *EnvironmentState
	lifecycleState         state.State
	lifecycleStore         io.Closer
	lifecycleEngine        *cosiruntime.Runtime
	lifecycleCancel        context.CancelFunc
	lifecycleDone          chan struct{}
	lifecycleRunErr        error
	lifecycleStartOnce     sync.Once
	lifecycleStartMu       sync.Mutex
	lifecycleInitErr       error
	lifecycleContext       context.Context
	lifecycleWorkers       sync.WaitGroup
	lifecycleCoordinator   *runtimeLifecycleController
	lifecycleControllerErr error
	processes              *supervision.Host
	scripts                *scripts.Runner
	bootstrapHost          *bootstrap.Host
	executionLifecycle     *executionLifecycle
	desired                lifecycle.EnvironmentDesired
	bootID                 string
	supervisor             *supervision.Supervisor
	admissionOpen          bool
	executorProxy          executorpb.ExecutorServer
	executionOwner         *executionLifetime
	activeAdmissions       uint64
	activeExecutionCalls   uint64
	execStreamID           uint64
	execStreams            map[uint64]context.CancelFunc
	execCalls              map[uint64]context.CancelFunc
	execCallID             uint64
	fenceInFlight          uint64
	fenceDone              chan struct{}
	fenceErr               error
	closing                bool
	failed                 bool
	changes                chan struct{}
	fatal                  chan error
	failureErr             error
	control                *control.Server
	bootstrapWait          func(context.Context) error
	sessions               *session.Manager
	store                  *retained.Store
	codexAppServer         *codexappserver.Host
	createMu               sync.Mutex
	sealPublishMu          sync.Mutex
	idleSince              *timestamppb.Timestamp
	startupComplete        bool
	harnessSupervisor      *supervision.Supervisor
	workspaceSupervisor    *supervision.Supervisor
	executionSupervisor    *supervision.Supervisor
	harnessProcesses       *supervision.Host
	sessionWorkers         sync.WaitGroup
	sessionScripts         *scripts.Runner
	sessionWorkerDone      chan struct{}
	sessionWorkerErr       error
	ingress                *ingressWorker
	closeOnce              sync.Once
	closeDone              chan struct{}
	closeErr               error
}

func New(config Config) (*Runtime, error) {
	return newRuntime(config, nil, runtimeDeps{})
}

// newRuntime is the deterministic test seam used by lifecycle/forwarding tests. A nil COSI state
// selects the production Runtime lifecycle store; a nonnil one lets focused tests inject controller
// state without changing the production process topology.
func newRuntime(config Config, injectedState state.State, deps runtimeDeps) (*Runtime, error) {
	if !filepath.IsAbs(config.StateDir) {
		return nil, errors.New("environment state directory must be absolute")
	}
	if config.Launch.Sandbox.Namespace == "" || config.Launch.Sandbox.Name == "" || config.Launch.Sandbox.UID == "" {
		return nil, errors.New("environment Runtime requires Sandbox namespace, name and UID")
	}
	if err := configuration.ValidateControlIdentity(config.Control, config.Launch.Sandbox); err != nil {
		return nil, err
	}
	if config.Supervision != "pid1" && config.Supervision != "subprocess" && config.Supervision != "systemd" {
		return nil, errors.New("environment Runtime requires explicit pid1, subprocess or systemd supervision")
	}
	if config.Supervision == "pid1" && (!config.PodPID1 || config.VMProcessLauncher != nil) {
		return nil, errors.New("PID1 Runtime supervision requires Pod PID1 configuration")
	}
	if config.Supervision == "subprocess" && (config.PodPID1 || config.VMProcessLauncher != nil) {
		return nil, errors.New("subprocess Runtime supervision requires Pod subprocess configuration")
	}
	if config.Supervision == "systemd" && (config.PodPID1 || config.VMProcessLauncher == nil) {
		return nil, errors.New("systemd Runtime supervision requires VM launcher configuration")
	}
	if config.Supervision == "pid1" && os.Getpid() != 1 && deps.prepareSupervisor == nil {
		return nil, errors.New("Pod environment Runtime must be PID 1")
	}
	if config.CodexExecServer != nil && (config.CodexBinary == "" || config.HarnessEnvironment == nil) {
		return nil, errors.New("execution service requires Codex binary and resolved harness environment")
	}
	if err := ensureDirectory(config.StateDir); err != nil {
		return nil, err
	}
	// Acquire the backing store before preparing any native supervision. The
	// BoltDB lock excludes another Runtime for the lifetime of this state.
	lifecycleState := injectedState
	var lifecycleStore io.Closer
	var err error
	if lifecycleState == nil {
		lifecycleState, lifecycleStore, err = cosistate.Open(config.StateDir, lifecycle.Namespace, bootstrap.SelectionRegistration(), bootstrap.EventRegistration(), retained.SessionRecordRegistration(), retained.IngressAckRegistration(), cosistate.EnvironmentSealRegistration())
		if err != nil {
			return nil, fmt.Errorf("open Runtime COSI state: %w", err)
		}
	}
	// Only subprocess helpers need an inherited owner descriptor. PID1 and
	// systemd use their outer process-domain fence after Runtime death.
	var owner *environmentOwner
	var ownerFile *os.File
	if config.Supervision == "subprocess" {
		owner, err = openEnvironmentOwner(config.StateDir)
		if err != nil {
			if lifecycleStore != nil {
				err = errors.Join(err, lifecycleStore.Close())
			}
			return nil, err
		}
		ownerFile = owner.file
	}
	supervisor := (*supervision.Supervisor)(nil)
	if deps.prepareSupervisor != nil {
		supervisor, err = deps.prepareSupervisor(config, ownerFile)
	} else if config.Supervision == "pid1" {
		supervisor, err = supervision.NewPID1Supervisor()
	} else if config.Supervision == "subprocess" {
		supervisor = supervision.NewSubprocessSupervisor(ownerFile)
	}
	if err != nil {
		if lifecycleStore != nil {
			err = errors.Join(err, lifecycleStore.Close())
		}
		return nil, errors.Join(err, owner.Close())
	}
	environmentState, err := NewEnvironmentState(lifecycleState)
	if err != nil {
		if lifecycleStore != nil {
			_ = lifecycleStore.Close()
		}
		_ = owner.Close()
		return nil, err
	}
	runtimeCtx, cancelRuntime := context.WithCancel(context.Background())
	r := &Runtime{
		config: config, deps: deps, owner: owner, state: environmentState, supervisor: supervisor,
		lifecycleState: lifecycleState, lifecycleStore: lifecycleStore, lifecycleCancel: cancelRuntime, lifecycleDone: make(chan struct{}), lifecycleContext: runtimeCtx,
		bootID: uuid.NewString(), changes: make(chan struct{}), fatal: make(chan error, 1), bootstrapWait: deps.waitBootstrap,
		desired: lifecycle.EnvironmentDesired{Mode: lifecycle.Inactive, Generation: 1}, closeDone: make(chan struct{}),
		execStreams: make(map[uint64]context.CancelFunc), execCalls: make(map[uint64]context.CancelFunc),
	}
	if r.deps.startExecution == nil {
		r.deps.startExecution = r.startExecutionService
	}
	if err := r.initializeLifecycle(runtimeCtx); err != nil {
		cancelRuntime()
		if lifecycleStore != nil {
			_ = lifecycleStore.Close()
		}
		_ = owner.Close()
		return nil, err
	}
	r.control = control.New(control.Config{
		SandboxUID: config.Launch.Sandbox.UID, BootID: r.bootID,
		ListenAddress: config.Control.ListenAddress, Security: config.Control.Security,
		IsClosing: r.isClosing, AdoptIncarnation: r.adoptIncarnation,
		Facts: r.facts, Changes: r.changesSince,
	}, r)
	r.executorProxy = newExecutionProxy(r)
	return r, nil
}

func (r *Runtime) initializeLifecycle(ctx context.Context) error {
	engine, err := cosiruntime.NewRuntime(r.lifecycleState, zap.L().Named("environment-runtime-lifecycle"))
	if err != nil {
		return err
	}
	r.lifecycleEngine = engine
	r.processes = supervision.NewHost(ctx, r.lifecycleState, lifecycle.Namespace, supervision.RuntimeDomain, &r.lifecycleWorkers, r.fail)
	if r.config.Supervision == "systemd" {
		r.supervisor, err = r.processes.DeclareSystemdScope(ctx, r.config.VMProcessLauncher, r.config.StateDir, r.config.Launch.Sandbox.UID, supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: RuntimeScopeID,
		})
		if err != nil {
			return err
		}
		r.harnessSupervisor, err = r.processes.DeclareSystemdScope(ctx, r.config.VMProcessLauncher, r.config.StateDir, r.config.Launch.Sandbox.UID, supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: HarnessScopeID,
		})
		if err != nil {
			return err
		}
		if r.config.CodexExecServer != nil {
			r.executionSupervisor, err = r.processes.DeclareSystemdScope(ctx, r.config.CodexExecServer.Launcher, filepath.Join(r.config.StateDir, "execution"), r.config.Launch.Sandbox.UID, supervision.SystemdOwner{
				Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: "execution-service",
			})
			if err != nil {
				return err
			}
		}
	} else {
		r.harnessSupervisor = r.supervisor
	}
	if err := engine.RegisterQController(r.processes.SystemdScopeController()); err != nil {
		return err
	}

	r.scripts, err = scripts.New(scripts.Config{
		HarnessEnvironment: r.config.HarnessEnvironment,
		LaunchProcess: func(processCtx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			r.mu.Lock()
			generation := r.desired.Generation
			r.mu.Unlock()
			return r.processes.Launch(processCtx, owner, generation, r.supervisor, processConfig)
		},
	})
	if err != nil {
		return err
	}
	r.bootstrapHost, err = bootstrap.NewHost(bootstrap.Config{
		Script: r.config.Launch.Bootstrap, StateDir: r.config.StateDir,
		WorkingDirectory: r.config.InitializationCwd,
	}, bootstrap.Dependencies{
		Context: ctx, State: r.lifecycleState, Workers: &r.lifecycleWorkers, Scripts: r.scripts,
		Admission: r.admitLifecycleEffect, OnError: r.fail, OnRuntimeChange: r.changed,
	})
	if err != nil {
		return err
	}
	if err := r.bootstrapHost.Initialize(ctx); err != nil {
		return err
	}
	if r.config.CodexExecServer != nil {
		r.executionLifecycle = &executionLifecycle{r: r, ctx: ctx, state: r.lifecycleState}
		if err := r.executionLifecycle.initialize(ctx); err != nil {
			return err
		}
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, r.lifecycleState, r.desired); err != nil {
		return err
	}
	if err := engine.RegisterQController(r.processes.Controller()); err != nil {
		return err
	}
	if err := engine.RegisterQController(r.bootstrapHost.Controller()); err != nil {
		return err
	}
	r.lifecycleCoordinator = &runtimeLifecycleController{r: r, transition: runtimeTransitionSequence{r: r}, requests: make(chan *runtimeTransitionRequest), closeDone: make(chan struct{}), stopped: make(chan struct{})}
	if err := engine.RegisterController(r.lifecycleCoordinator); err != nil {
		return err
	}
	if r.executionLifecycle != nil {
		if err := engine.RegisterQController(&executionController{host: r.executionLifecycle}); err != nil {
			return err
		}
	}
	return nil
}

func (r *Runtime) startLifecycleEngine() {
	r.lifecycleStartMu.Lock()
	defer r.lifecycleStartMu.Unlock()
	r.startLifecycleEngineLocked()
}

// Caller holds lifecycleStartMu, which excludes Session controller registration from Close.
func (r *Runtime) startLifecycleEngineLocked() {
	r.lifecycleStartOnce.Do(func() {
		go func() {
			defer close(r.lifecycleDone)
			// A dead engine cannot advance process finalizers. Cancel its host
			// lifetime so managed process waiters fail instead of waiting forever;
			// the outer Runtime retains ownership when cleanup is unproven.
			defer r.lifecycleCancel()
			if err := r.lifecycleEngine.Run(r.lifecycleContext); r.lifecycleContext.Err() == nil {
				if err == nil {
					err = errors.New("engine returned without shutdown")
				}
				runErr := fmt.Errorf("environment Runtime lifecycle engine stopped: %w", err)
				r.mu.Lock()
				r.lifecycleRunErr = runErr
				r.mu.Unlock()
				r.fail(runErr)
			}
		}()
	})
}

// Close stops Runtime-owned bootstrap work and releases the environment-owner lock.
// Canceling ctx only stops this caller's wait; shutdown and worker joining continue.
func (r *Runtime) Close(ctx context.Context) error {
	r.mu.Lock()
	r.closing = true
	r.admissionOpen = false
	if r.activeTransitionCancel != nil {
		r.activeTransitionCancel()
	}
	r.changedLocked()
	r.mu.Unlock()
	if r.executionLifecycle != nil {
		r.executionLifecycle.cancelStart()
	}
	r.closeOnce.Do(func() {
		go func() {
			r.lifecycleStartMu.Lock()
			if r.lifecycleInitErr != nil {
				r.closeErr = fmt.Errorf("Session construction left lifecycle engine unsafe to run: %w", r.lifecycleInitErr)
				r.lifecycleStartMu.Unlock()
			} else {
				r.startLifecycleEngineLocked()
				r.lifecycleStartMu.Unlock()
				r.closeErr = r.finishClose()
			}
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

func (r *Runtime) finishClose() error {
	// Settle the accepted transition and any quiescence fence before publishing a
	// successor shutdown generation. A late old fence must never fail or overwrite
	// the generation that Close is about to own.
	r.transitionMu.Lock()
	r.mu.Lock()
	priorFence := r.fenceDone
	r.mu.Unlock()
	if priorFence != nil {
		<-priorFence
	}

	r.mu.Lock()
	desired := r.desired
	desired.Mode, desired.Generation = lifecycle.Closing, desired.Generation+1
	if sealedUID, sealed, err := r.state.SealedUID(); err != nil {
		r.mu.Unlock()
		r.transitionMu.Unlock()
		return err
	} else if sealed {
		desired.SealUID = sealedUID
		desired.IncarnationUID = sealedUID
	}
	r.admissionOpen = false
	if err := lifecycle.PublishEnvironmentDesired(context.Background(), r.lifecycleState, desired); err != nil {
		r.mu.Unlock()
		r.transitionMu.Unlock()
		return fmt.Errorf("publish Runtime closing state: %w", err)
	}
	r.desired = desired
	r.changedLocked()
	r.mu.Unlock()
	r.transitionMu.Unlock()

	// The coordinator's result names the exact generation; completion only
	// permits joining the engine, and never proves owner release on its own.
	select {
	case <-r.lifecycleCoordinator.closeDone:
		result := r.lifecycleCoordinator.closeProgress()
		if result.generation != desired.Generation {
			return fmt.Errorf("Runtime Close completed generation %d, want %d", result.generation, desired.Generation)
		}
		if result.err != nil {
			return result.err
		}
		return r.joinClose()
	case <-r.lifecycleCoordinator.stopped:
		return r.coordinatorFailure("Close completion")
	case <-r.lifecycleDone:
		return r.coordinatorFailure("Close completion")
	}
}

func (r *Runtime) joinClose() error {
	// The aggregate has stopped every component that can need the engine. Its
	// completion means only that it is now safe to join that engine.
	r.lifecycleCancel()
	<-r.lifecycleDone
	r.mu.Lock()
	lifecycleRunErr := r.lifecycleRunErr
	r.mu.Unlock()
	if lifecycleRunErr != nil {
		return lifecycleRunErr
	}
	sessionLossErr := r.lifecycleCoordinator.plan.sessionLossErr
	if sessionLossErr != nil {
		r.mu.Lock()
		sessionLossErr = errors.Join(sessionLossErr, r.failureLocked())
		r.mu.Unlock()
	}
	if r.sessions != nil {
		<-r.sessionWorkerDone
		// Recovery completion does not prove native cleanup. Retain Runtime's
		// owner if the Session component cannot finish its joined work.
		if cleanupErr := r.closeSessions(context.Background()); cleanupErr != nil {
			return errors.Join(sessionLossErr, r.sessionWorkerErr, fmt.Errorf("Session cleanup unproved: %w", cleanupErr))
		}
		sessionLossErr = errors.Join(sessionLossErr, r.sessionWorkerErr)
	}
	r.lifecycleWorkers.Wait()
	if r.config.Supervision == "pid1" {
		if err := r.supervisor.FenceNamespace(context.Background()); err != nil {
			return fmt.Errorf("fence Pod PID namespace: %w", err)
		}
	}
	if r.lifecycleStore != nil {
		sessionLossErr = errors.Join(sessionLossErr, r.lifecycleStore.Close())
	}
	return errors.Join(sessionLossErr, r.owner.Close())
}

func (r *Runtime) waitBootstrapSettled(ctx context.Context) error {
	if r.bootstrapHost == nil || !r.bootstrapHost.Configured() {
		return nil
	}
	request, found, err := bootstrap.ReadRequest(ctx, r.lifecycleState)
	if err != nil {
		return fmt.Errorf("read accepted bootstrap state during shutdown: %w", err)
	}
	if !found || !request.Requested {
		return nil
	}
	// A terminal bootstrap error is its durable outcome, not a failure to fence the
	// Runtime-owned process. Settled checks that the accepted operation and cleanup ended.
	_ = r.bootstrapHost.Wait(ctx)
	if err := ctx.Err(); err != nil {
		return err
	}
	settled, err := r.bootstrapHost.Settled(ctx)
	if err != nil {
		return fmt.Errorf("observe accepted bootstrap cleanup: %w", err)
	}
	if !settled {
		return errors.New("accepted bootstrap did not settle before Runtime shutdown")
	}
	return nil
}

func (r *Runtime) changed() {
	r.mu.Lock()
	r.changedLocked()
	r.mu.Unlock()
}

func (r *Runtime) startExecutionForUID(ctx context.Context, uid string) error {
	if r.config.CodexExecServer == nil {
		return nil
	}
	demand, err := r.executionLifecycle.request(ctx, uid)
	if err != nil {
		return err
	}
	return r.executionLifecycle.wait(ctx, uid, demand)
}

func (r *Runtime) requestBootstrap(ctx context.Context) error {
	if r.bootstrapHost != nil && r.bootstrapHost.Configured() {
		if err := r.bootstrapHost.Request(ctx); err != nil {
			return fmt.Errorf("admit environment bootstrap: %w", err)
		}
	}
	r.changed()
	return nil
}

func (r *Runtime) stopExecution(ctx context.Context) error {
	if r.executionLifecycle == nil {
		r.mu.Lock()
		unmanaged := r.executionOwner != nil || r.config.CodexExecServer != nil
		r.mu.Unlock()
		if unmanaged {
			return errors.New("embedded Execution owner has no lifecycle controller or fence proof")
		}
		return nil
	}
	demand, err := r.executionLifecycle.request(context.Background(), "")
	if err != nil {
		return err
	}
	return r.executionLifecycle.wait(ctx, "", demand)
}

func (r *Runtime) facts() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
	facts, err := r.sessionFacts(context.Background())
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	desired, closing := r.desired, r.closing
	activeAdmissions := r.activeAdmissions
	activeExecution := r.activeExecutionCalls
	executionRecovering := r.executionOwner.recovering()
	ready := r.startupComplete && !r.failed
	r.mu.Unlock()
	if facts == nil {
		facts = &environmentruntimestatepb.EnvironmentRuntimeFacts{}
	}
	bootstrapActive := false
	if r.bootstrapHost != nil && r.bootstrapHost.Configured() {
		request, requestFound, err := bootstrap.ReadRequest(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapStatus, statusFound, err := bootstrap.ReadStatus(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapActive = bootstrapIsActive(request, requestFound, bootstrapStatus, statusFound)
		if bootstrapActive {
			// An accepted request is busy even before the lifecycle controller has
			// published Running. Do not expose its idle Session facts in that gap.
			facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_RUNNING
			if !hasAutosuspendReason(facts.AutosuspendBlockers, "Bootstrap") {
				facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "Bootstrap"})
			}
		} else if statusFound {
			switch bootstrapStatus.Phase {
			case bootstrap.Succeeded:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED
			case bootstrap.Failed:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED
			case bootstrap.Interrupted:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED
			}
		}
	}
	if (closing || desired.Mode == lifecycle.Closing || desired.Mode == lifecycle.Quiescing) && !ready {
		facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_DRAINING
	}
	if activeAdmissions > 0 && !hasAutosuspendReason(facts.AutosuspendBlockers, "Admission") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "Admission"})
	}
	if activeExecution > 0 && !hasAutosuspendReason(facts.AutosuspendBlockers, "ExecutorRelay") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "ExecutorRelay"})
	}
	if executionRecovering && !hasAutosuspendReason(facts.AutosuspendBlockers, "ExecutorRelay") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "ExecutorRelay"})
	}
	if activeAdmissions > 0 || activeExecution > 0 || executionRecovering || bootstrapActive || r.state.Lameduck() || desired.Mode != lifecycle.Running {
		facts.IdleSince = nil
	} else if desired.Mode == lifecycle.Running {
		// Idle observations are valid only after Session recovery.
		if !ready {
			facts.IdleSince = nil
		}
	}
	sort.Slice(facts.AutosuspendBlockers, func(i, j int) bool {
		return facts.AutosuspendBlockers[i].GetReason() < facts.AutosuspendBlockers[j].GetReason()
	})
	return facts, nil
}

func hasAutosuspendReason(blockers []*sandboxpb.AutosuspendBlocker, reason string) bool {
	for _, blocker := range blockers {
		if blocker.GetReason() == reason {
			return true
		}
	}
	return false
}

func bootstrapIsActive(request bootstrap.Request, requestFound bool, status bootstrap.Status, statusFound bool) bool {
	terminal := statusFound && (status.Phase == bootstrap.Succeeded || status.Phase == bootstrap.Failed || status.Phase == bootstrap.Interrupted)
	return (statusFound && status.Phase == bootstrap.Running) || (requestFound && request.Requested && !terminal)
}

func (r *Runtime) isClosing() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.closing
}

func (r *Runtime) changesSince() <-chan struct{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.changes
}

func (r *Runtime) changedLocked() {
	close(r.changes)
	r.changes = make(chan struct{})
}

func (r *Runtime) fail(err error) {
	if err == nil {
		return
	}
	r.mu.Lock()
	if r.failureErr == nil {
		r.failureErr = err
	}
	r.failed = true
	r.admissionOpen = false
	r.changedLocked()
	r.mu.Unlock()
	select {
	case r.fatal <- err:
	default:
	}
}

func (r *Runtime) failureLocked() error {
	if r.failureErr != nil {
		return r.failureErr
	}
	if r.failed {
		return errors.New("environment Runtime failed")
	}
	return nil
}

var _ environmentruntimepb.EnvironmentRuntimeServer = (*Runtime)(nil)
