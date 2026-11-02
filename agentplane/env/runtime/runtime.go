// Package runtime owns one Sandbox environment's identity, bootstrap and component lifecycles.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	RuntimeUnitName                            = "agentplane-environment-runtime.service"
	RuntimeScopeID  supervision.SystemdScopeID = supervision.SystemdEnvironmentRuntimeScopeID
	HostScopeID     supervision.SystemdScopeID = "harness-host"
)

// Config contains the resolved environment and embedded Host configuration.
type Config struct {
	Control            configuration.EnvironmentRuntimeControlConfiguration
	StateDir           string
	Launch             configuration.SandboxLaunch
	Supervision        string
	PodPID1            bool
	VMProcessLauncher  *supervision.SystemdLauncher
	CodexExecServer    *codexexecserver.Config
	CodexBinary        string
	HarnessEnvironment map[string]string
	InitializationCwd  string
	HostConfig         harnesshost.Config
	HostIngress        configuration.Endpoint
}

type hostLifetime interface {
	Stop(context.Context) error
	WaitClosed(context.Context) error
	Wait(context.Context) (int, error)
	Cleanup(context.Context) error
}

type childExit struct {
	code int
	err  error
}

type runtimeDeps struct {
	prepareSupervisor      func(Config, *os.File) (*supervision.Supervisor, error)
	waitBootstrap          func(context.Context) error
	startExecution         func(context.Context, Config, string) (executionHandle, error)
	beforeHostConstruction func()
}

type executionHandle interface {
	Executor() executorpb.ExecutorServer
	StopAndFence(context.Context) error
}

type hostLink struct {
	bootID    string
	client    *harnesshost.Runner
	peer      hostLifecyclePeer
	connected bool
}

// Runtime owns the embedded Host lifecycle and serves the Gateway-facing Runner API.
// Gateway epoch admission remains in env/runtime/control; every direct Host call
// carries the exact Runtime/Host/generation binding.
type Runtime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	config Config
	deps   runtimeDeps

	mu                       sync.Mutex
	transitionMu             sync.Mutex
	transitionUnsafe         error
	activeTransitionCtx      context.Context
	activeTransitionCancel   context.CancelFunc
	activeTransitionSequence uint64
	owner                    *environmentOwner
	state                    *EnvironmentState
	lifecycleState           state.State
	lifecycleEngine          *cosiruntime.Runtime
	lifecycleCancel          context.CancelFunc
	lifecycleDone            chan struct{}
	lifecycleRunErr          error
	lifecycleStartOnce       sync.Once
	lifecycleStartMu         sync.Mutex
	lifecycleInitErr         error
	lifecycleContext         context.Context
	lifecycleWorkers         sync.WaitGroup
	closeCoordinator         *runtimeCloseController
	closeControllerErr       error
	processes                *supervision.Host
	scripts                  *scripts.Runner
	bootstrapHost            *bootstrap.Host
	executionLifecycle       *executionLifecycle
	desired                  lifecycle.EnvironmentDesired
	bootID                   string
	supervisor               *supervision.Supervisor
	host                     *hostLink
	intent                   *runtimepb.LifecycleIntent
	lastRunning              *runtimepb.Binding
	executorProxy            executorpb.ExecutorServer
	executionOwner           *executionLifetime
	activeAdmissions         uint64
	activeExecutionCalls     uint64
	execStreamID             uint64
	execStreams              map[uint64]context.CancelFunc
	execCalls                map[uint64]context.CancelFunc
	execCallID               uint64
	fenceInFlight            *runtimepb.Binding
	closing                  bool
	failed                   bool
	changes                  chan struct{}
	fatal                    chan error
	failureErr               error
	control                  *control.Server
	waitBootstrap            func(context.Context) error
	hostLifetime             hostLifetime
	hostExited               bool
	hostExit                 chan childExit
	closeOnce                sync.Once
	closeDone                chan struct{}
	closeErr                 error
}

func New(config Config) (*Runtime, error) {
	return newRuntime(config, nil, runtimeDeps{})
}

// newRuntime is the deterministic test seam used by lifecycle/forwarding tests. A nil COSI state
// selects an in-memory Runner lifecycle store; a nonnil one lets focused tests inject controller
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
	owner, err := openEnvironmentOwner(config.StateDir)
	if err != nil {
		return nil, err
	}
	environmentState, err := NewEnvironmentState(config.StateDir)
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	supervisor := (*supervision.Supervisor)(nil)
	if deps.prepareSupervisor != nil {
		supervisor, err = deps.prepareSupervisor(config, owner.file)
	} else if config.Supervision == "pid1" {
		supervisor, err = supervision.NewPID1Supervisor(owner.file)
	} else if config.Supervision == "subprocess" {
		supervisor = supervision.NewSubprocessSupervisor(owner.file)
	} else {
		supervisor, err = config.VMProcessLauncher.PrepareForOwner(config.StateDir, config.Launch.Sandbox.UID, supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: RuntimeScopeID,
		})
	}
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	lifecycleState := injectedState
	if lifecycleState == nil {
		lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	}
	runtimeCtx, cancelRuntime := context.WithCancel(context.Background())
	r := &Runtime{
		config: config, deps: deps, owner: owner, state: environmentState, supervisor: supervisor,
		lifecycleState: lifecycleState, lifecycleCancel: cancelRuntime, lifecycleDone: make(chan struct{}), lifecycleContext: runtimeCtx,
		bootID: uuid.NewString(), changes: make(chan struct{}), fatal: make(chan error, 1), waitBootstrap: deps.waitBootstrap,
		desired: lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 1}, closeDone: make(chan struct{}),
		execStreams: make(map[uint64]context.CancelFunc), execCalls: make(map[uint64]context.CancelFunc),
	}
	if r.deps.startExecution == nil {
		r.deps.startExecution = r.startExecutionService
	}
	if err := r.initializeLifecycle(runtimeCtx); err != nil {
		cancelRuntime()
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
	r.processes = supervision.NewHost(ctx, r.lifecycleState, lifecycle.EnvironmentNamespace, &r.lifecycleWorkers, r.fail)
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
	r.closeCoordinator = &runtimeCloseController{r: r, transition: runtimeTransitionSequence{r: r}}
	if err := engine.RegisterController(r.closeCoordinator); err != nil {
		return err
	}
	if err := engine.RegisterController(&hostReportController{runtime: r}); err != nil {
		return err
	}
	if err := engine.RegisterController(&hostIntentController{runtime: r}); err != nil {
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

// Caller holds lifecycleStartMu, which excludes Host registration from Close.
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

// admitLifecycleEffect serializes bootstrap admission with Runtime generation changes.
func (r *Runtime) admitLifecycleEffect(ctx context.Context, expected lifecycle.EnvironmentDesired, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	if r.closing || r.desired != expected || r.desired.Mode != lifecycle.Running {
		return nil
	}
	return effect(ctx)
}

// Close stops Runtime-owned bootstrap work and releases the legacy environment-owner lock.
// Canceling ctx only stops this caller's wait; shutdown and worker joining continue.
func (r *Runtime) Close(ctx context.Context) error {
	r.mu.Lock()
	r.closing = true
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
				r.closeErr = fmt.Errorf("Harness Host construction left lifecycle engine unsafe to run: %w", r.lifecycleInitErr)
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
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runtime) finishClose() error {
	r.mu.Lock()
	desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: r.desired.Generation + 1}
	if err := lifecycle.PublishEnvironmentDesired(context.Background(), r.lifecycleState, desired); err != nil {
		r.mu.Unlock()
		return fmt.Errorf("publish Runtime closing state: %w", err)
	}
	r.desired = desired
	r.changedLocked()
	r.mu.Unlock()

	// The aggregate controller owns all component shutdown steps. Close waits for
	// its exact generation; a failed or superseded generation cannot release the
	// environment owner. Caller cancellation affects only the outer wait.
	for {
		r.mu.Lock()
		changed := r.changes
		controllerErr := r.closeControllerErr
		r.mu.Unlock()
		if controllerErr != nil {
			return controllerErr
		}
		current, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
		if err != nil {
			return fmt.Errorf("read Runtime Close status: %w", err)
		}
		if found && current.Generation == desired.Generation {
			switch current.Stage {
			case lifecycle.RuntimeCloseFailed:
				return errors.New(current.Error)
			case lifecycle.RuntimeCloseReadyToJoin:
				return r.joinClose()
			}
		}
		select {
		case <-changed:
		case <-r.lifecycleDone:
			r.mu.Lock()
			failure := r.failureLocked()
			r.mu.Unlock()
			if failure != nil {
				return failure
			}
			return errors.New("Runtime lifecycle engine exited before Close completed")
		}
	}
}

func (r *Runtime) joinClose() error {
	// The aggregate has stopped every component that can need the engine. Its
	// terminal status means only that it is now safe to join that engine.
	r.lifecycleCancel()
	<-r.lifecycleDone
	r.mu.Lock()
	lifecycleRunErr := r.lifecycleRunErr
	r.mu.Unlock()
	if lifecycleRunErr != nil {
		return lifecycleRunErr
	}
	hostRunErr := r.closeCoordinator.plan.hostRunErr
	r.mu.Lock()
	host := r.hostLifetime
	r.mu.Unlock()
	if host != nil {
		code, runErr := host.Wait(context.Background())
		if code != 0 && runErr == nil {
			runErr = fmt.Errorf("Harness Host exited with status %d", code)
		}
		// Host.Run also reports bridge loss. Native cleanup is a separate proof:
		// retain Runtime's owner if Host.Close could not finish its joined work.
		if cleanupErr := host.Cleanup(context.Background()); cleanupErr != nil {
			return errors.Join(hostRunErr, runErr, fmt.Errorf("Harness Host cleanup unproved: %w", cleanupErr))
		}
		hostRunErr = errors.Join(hostRunErr, runErr)
	}
	r.lifecycleWorkers.Wait()
	if r.config.Supervision == "pid1" {
		if err := r.supervisor.FenceNamespace(context.Background()); err != nil {
			return fmt.Errorf("fence Pod PID namespace: %w", err)
		}
	}
	if r.bootstrapHost != nil {
		if err := r.bootstrapHost.CloseLog(); err != nil {
			return err
		}
	}
	return errors.Join(hostRunErr, r.owner.Close())
}

func (r *Runtime) waitBootstrapSettled(ctx context.Context) error {
	if r.bootstrapHost == nil || !r.bootstrapHost.Configured() {
		return nil
	}
	input, found, err := bootstrap.ReadInput(ctx, r.lifecycleState)
	if err != nil {
		return fmt.Errorf("read accepted bootstrap state during shutdown: %w", err)
	}
	if !found || !input.Requested {
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

func (r *Runtime) stopHost(ctx context.Context) error {
	r.mu.Lock()
	process := r.hostLifetime
	r.mu.Unlock()
	if process == nil {
		return nil
	}
	stopErr := process.Stop(ctx)
	if stopErr != nil {
		return stopErr
	}
	return process.WaitClosed(ctx)
}

func (r *Runtime) changed() {
	r.mu.Lock()
	r.changedLocked()
	r.mu.Unlock()
}

func (r *Runtime) waitForHostNativeStopped(ctx context.Context, binding *runtimepb.Binding) error {
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		host := r.host
		if host == nil || !host.connected {
			r.mu.Unlock()
			return errors.New("Harness Host lifecycle connection was lost before native-stop acknowledgement")
		}
		report := r.hostReportLocked()
		if report != nil && report.Ready && report.NativeStopped && proto.Equal(report.Binding, binding) {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
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

func (r *Runtime) publishIntent(ctx context.Context, desiredMode lifecycle.Mode, intentMode runtimepb.LifecycleIntent_Mode, uid, sealUID string) (*runtimepb.Binding, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || r.host == nil || !r.host.connected {
		return nil, status.Error(codes.Unavailable, "environment Runtime or Harness Host is closing")
	}
	desired := lifecycle.EnvironmentDesired{Mode: desiredMode, SealUID: sealUID, Generation: r.desired.Generation + 1}
	if err := lifecycle.PublishEnvironmentDesired(ctx, r.lifecycleState, desired); err != nil {
		return nil, status.Errorf(codes.Unavailable, "publish environment lifecycle generation: %v", err)
	}
	r.desired = desired
	intent := r.setIntentLocked(intentMode, uid, sealUID, false)
	if intentMode != runtimepb.LifecycleIntent_RUNNING && r.executionLifecycle != nil {
		r.executionLifecycle.cancelStart()
	}
	if intentMode == runtimepb.LifecycleIntent_RUNNING {
		r.lastRunning = proto.Clone(intent.Binding).(*runtimepb.Binding)
	}
	return proto.Clone(intent.Binding).(*runtimepb.Binding), nil
}

func (r *Runtime) setLastRunning(binding *runtimepb.Binding) {
	r.mu.Lock()
	r.lastRunning = proto.Clone(binding).(*runtimepb.Binding)
	r.mu.Unlock()
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
	r.mu.Lock()
	var facts *environmentruntimestatepb.EnvironmentRuntimeFacts
	hostReady := false
	report := r.hostReportLocked()
	intentPresent := r.host != nil && r.intent != nil && report != nil && proto.Equal(report.Binding, r.intent.Binding)
	if intentPresent {
		hostReady = report.Ready
	}
	if hostReady && report.Facts != nil {
		facts = proto.Clone(report.Facts).(*environmentruntimestatepb.EnvironmentRuntimeFacts)
	}
	desired, closing := r.desired, r.closing
	activeAdmissions := r.activeAdmissions
	activeExecution := r.activeExecutionCalls
	executionRecovering := r.executionOwner.recovering()
	r.mu.Unlock()
	if facts == nil {
		facts = &environmentruntimestatepb.EnvironmentRuntimeFacts{}
	}
	bootstrapActive := false
	if r.bootstrapHost != nil && r.bootstrapHost.Configured() {
		input, inputFound, err := bootstrap.ReadInput(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapState, runtimeFound, err := bootstrap.ReadRuntime(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapActive = bootstrapIsActive(input, inputFound, bootstrapState, runtimeFound)
		if bootstrapActive {
			// A requested input is busy even before the lifecycle controller has
			// published Running. Do not expose its idle Host facts in that gap.
			facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_RUNNING
			if !hasAutosuspendReason(facts.AutosuspendBlockers, "Bootstrap") {
				facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "Bootstrap"})
			}
		} else if runtimeFound {
			switch bootstrapState.Phase {
			case bootstrap.Succeeded:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED
			case bootstrap.Failed:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED
			case bootstrap.Interrupted:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED
			}
		}
	}
	if (closing || desired.Mode == lifecycle.Closing || desired.Mode == lifecycle.Quiescing) && !hostReady {
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
		// The Host's idle timestamp is valid only while the active environment binding matches.
		if !intentPresent || !hostReady {
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

func bootstrapIsActive(input bootstrap.Input, inputFound bool, state bootstrap.Runtime, stateFound bool) bool {
	terminal := stateFound && (state.Phase == bootstrap.Succeeded || state.Phase == bootstrap.Failed || state.Phase == bootstrap.Interrupted)
	return (stateFound && state.Phase == bootstrap.Running) || (inputFound && input.Requested && !terminal)
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

func (r *Runtime) intentChangedLocked() {
	if err := r.publishHostIntentLocked(); err != nil {
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = err
		}
		select {
		case r.fatal <- err:
		default:
		}
	}
	r.changedLocked()
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

func (r *Runtime) closeOwner() error { return r.owner.Close() }

var (
	_ environmentruntimepb.EnvironmentRuntimeServer = (*Runtime)(nil)
	_ harnesshost.RuntimeBridge                     = (*Runtime)(nil)
)
