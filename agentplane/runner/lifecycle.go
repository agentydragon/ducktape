package runner

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/cosi-project/runtime/pkg/controller"
	// The COSI runtime shares its package name with the runner's runtime state.
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/google/uuid"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

const (
	lifecycleNamespace    = "runner"
	runnerGoalType        = "RunnerGoals.agentplane"
	runnerInputType       = "RunnerInputs.agentplane"
	runnerRuntimeType     = "RunnerRuntimes.agentplane"
	sessionInputType      = "SessionInputs.agentplane"
	sessionRuntimeType    = "SessionRuntimes.agentplane"
	journalSealType       = "JournalSeals.agentplane"
	ingressAckType        = "IngressAcks.agentplane"
	operationResultType   = "NativeOperationResults.agentplane"
	runnerControllerName  = "runner-lifecycle"
	sessionControllerName = "session-lifecycle"
)

// Specs contain values only; native handles and cancellation belong to operation hosts.
type lifecycleResource[T comparable] struct {
	metadata resource.Metadata
	value    T
}

func (r *lifecycleResource[T]) Metadata() *resource.Metadata { return &r.metadata }
func (r *lifecycleResource[T]) Spec() any                    { return &r.value }
func (r *lifecycleResource[T]) DeepCopy() resource.Resource {
	return &lifecycleResource[T]{metadata: r.metadata.Copy(), value: r.value}
}

func lifecycleResourceFor[T comparable](kind, id string, value T) *lifecycleResource[T] {
	return &lifecycleResource[T]{metadata: resource.NewMetadata(lifecycleNamespace, kind, id, resource.VersionUndefined), value: value}
}

type lifecycleReader interface {
	Get(context.Context, resource.Pointer, ...state.GetOption) (resource.Resource, error)
}

func lifecycleRead[T comparable](ctx context.Context, reader lifecycleReader, kind, id string) (T, bool, error) {
	var zero T
	r, err := reader.Get(ctx, resource.NewMetadata(lifecycleNamespace, kind, id, resource.VersionUndefined))
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}
	typed, ok := r.(*lifecycleResource[T])
	if !ok {
		return zero, false, fmt.Errorf("unexpected lifecycle resource %T for %s/%s", r, kind, id)
	}
	return typed.value, true, nil
}

func lifecycleWrite[T comparable](ctx context.Context, writer controller.ReaderWriter, kind, id string, value T) (bool, error) {
	old, found, err := lifecycleRead[T](ctx, writer, kind, id)
	if err != nil || found && old == value {
		return false, err
	}
	err = writer.Modify(ctx, lifecycleResourceFor(kind, id, value), func(r resource.Resource) error {
		r.(*lifecycleResource[T]).value = value
		return nil
	})
	return err == nil, err
}

type runnerMode uint8

const (
	runnerRunning runnerMode = iota
	runnerQuiescing
	runnerClosing
)

type runnerStage uint8

const (
	stageRunning runnerStage = iota
	stageDraining
	stageHalting
	stageStopping
	stageFencing
	stageSealing
	stageFlushing
	stageQuiesced
	stageClosed
)

type runnerGoalSpec struct {
	Mode       runnerMode
	SealUID    string
	Generation uint64
}
type runnerInputSpec struct {
	Revision   uint64
	Admissions int
}
type runnerRuntimeSpec struct {
	Generation uint64
	Stage      runnerStage
	Error      string
}
type executionPhase uint8

const (
	executionDormant executionPhase = iota
	executionPreparing
	executionReady
	executionStopping
	executionHalted
)

type sessionRuntimeSpec struct {
	Phase          executionPhase
	Attempt        uint64
	Resuming       bool
	ProcessID      string
	Generation     uint64
	SetupAttempt   string
	ProcessStopped bool
	Error          string
}
type sessionInputSpec struct {
	Demand            uint64
	Cursor            uint64
	SetupRequested    bool
	Setup             runnerpb.SetupState
	ActiveTurn        string
	Pending           int
	ProcessID         string
	ProcessRunning    bool
	Inspections       int
	InspectionRunning bool
	InspectionReady   bool
}
type operationResultSpec struct {
	Session string
	Kind    string
	Done    bool
	Error   string
}
type journalSealSpec struct {
	Present        bool
	IncarnationUID string
	Cursor         uint64
}
type ingressAckSpec struct {
	Cursor uint64
	Sealed bool
}

func (l *lifecycle) publishSeal(s *Session) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := journalSealSpec{}
	if seal := s.journal.SealState(); seal != nil {
		value = journalSealSpec{Present: true, IncarnationUID: seal.IncarnationUID, Cursor: seal.Cursor}
	}
	return publishLifecycle(l, journalSealType, s.id, value)
}

func (l *lifecycle) publishAck(id string, ack IngressAcknowledgement) error {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	return publishLifecycle(l, ingressAckType, id, ingressAckSpec{Cursor: ack.Cursor, Sealed: ack.Sealed})
}

type stopReason uint8

const (
	stopNone stopReason = iota
	stopStartupFailure
	stopClose
	stopQuiesce
)

// done closes only after err is installed under the host mutex. Slots remain owned
// until the controller consumes the result, preventing duplicate native effects.
type sessionOperation struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func operationDone(op *sessionOperation) bool {
	if op == nil {
		return false
	}
	select {
	case <-op.done:
		return true
	default:
		return false
	}
}

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
	for _, c := range []controller.QController{&executorLifecycleController{l: l}, &codexLifecycleController{l: l}, &bootstrapLifecycleController{l: l}} {
		if err := engine.RegisterQController(c); err != nil {
			return nil, err
		}
	}
	if err := publishLifecycle(l, executorInputType, "executor", executorInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, codexInputType, "codex", codexInputSpec{}); err != nil {
		return nil, err
	}
	if err := publishLifecycle(l, bootstrapInputType, "bootstrap", bootstrapInputSpec{}); err != nil {
		return nil, err
	}

	if err := publishLifecycle(l, runnerGoalType, "runner", runnerGoalSpec{}); err != nil {
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

// Host publishers serialize read/modify/write; COSI controllers own different outputs.
func publishLifecycle[T comparable](l *lifecycle, kind, id string, value T) error {
	r := lifecycleResourceFor(kind, id, value)
	old, err := l.state.Get(l.runner.ctx, r.Metadata())
	if state.IsNotFoundError(err) {
		return l.state.Create(l.runner.ctx, r, state.WithCreateOwner("runner-host"))
	}
	if err != nil {
		return err
	}
	if old.(*lifecycleResource[T]).value == value {
		return nil
	}
	r.metadata = old.Metadata().Copy()
	return l.state.Update(l.runner.ctx, r, state.WithUpdateOwner("runner-host"))
}

func (l *lifecycle) goal() runnerGoalSpec {
	value, _, err := lifecycleRead[runnerGoalSpec](l.runner.ctx, l.state, runnerGoalType, "runner")
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
			input, _, err := lifecycleRead[sessionInputSpec](l.runner.ctx, l.state, sessionInputType, id)
			if err != nil {
				l.publishMu.Unlock()
				return err
			}
			input.Demand = 0
			if err := publishLifecycle(l, sessionInputType, id, input); err != nil {
				l.publishMu.Unlock()
				return err
			}
		}
		if err := publishLifecycle(l, codexInputType, "codex", codexInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		if err := publishLifecycle(l, executorInputType, "executor", executorInputSpec{}); err != nil {
			l.publishMu.Unlock()
			return err
		}
		l.publishMu.Unlock()
	}
	if goal.Mode != mode || goal.SealUID != sealUID || retry {
		goal.Mode, goal.SealUID, goal.Generation = mode, sealUID, goal.Generation+1
	}
	return publishLifecycle(l, runnerGoalType, "runner", goal)
}

func (l *lifecycle) accepting() bool {
	return l.goal().Mode == runnerRunning && !l.runner.store.Lameduck()
}

func (l *lifecycle) touchRunner() {
	l.runner.mu.Lock()
	admissions := l.runner.admissions
	l.runner.mu.Unlock()
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[runnerInputSpec](l.runner.ctx, l.state, runnerInputType, "runner")
	if err != nil {
		l.runner.fail(err)
		return
	}
	input.Admissions = admissions
	input.Revision++
	l.runner.fail(publishLifecycle(l, runnerInputType, "runner", input))
}

func (l *lifecycle) publishSession(s *Session, demand bool) {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, _, err := lifecycleRead[sessionInputSpec](l.runner.ctx, l.state, sessionInputType, s.id)
	if err != nil {
		l.runner.fail(err)
		return
	}
	if demand {
		input.Demand++
	}
	s.mu.Lock()
	input.SetupRequested = s.setupScript != nil
	input.Setup, input.ActiveTurn, input.Pending, input.ProcessID = s.setup, s.activeTurn, len(s.pending), s.processID
	input.ProcessRunning = s.process != nil && s.process.Running()
	input.InspectionRunning = s.inspection != nil
	input.Inspections = len(s.reconciliations)
	input.InspectionReady = len(s.reconciliations) > 0 && s.reconciliations[0].ready
	s.mu.Unlock()
	input.Cursor = s.journal.LastCursor()
	l.runner.fail(publishLifecycle(l, sessionInputType, s.id, input))
}
func (s *Session) wake()             { s.runner.lifecycle.publishSession(s, false) }
func (s *Session) requestExecution() { s.runner.lifecycle.publishSession(s, true) }
func (s *Session) runtimeState() sessionRuntimeSpec {
	value, _, err := lifecycleRead[sessionRuntimeSpec](s.runner.ctx, s.runner.lifecycle.state, sessionRuntimeType, s.id)
	if err != nil {
		s.runner.fail(err)
	}
	return value
}

func (s *Session) readyLocked() bool {
	current := s.runtimeState()
	return !operationDone(s.halting) && (current.Phase == executionReady || current.Phase == executionStopping) && current.ProcessID == s.processID && s.process != nil && s.process.Running()
}

func (s *Session) executionClosed() bool {
	return !s.runner.lifecycle.accepting() || s.journal.SealState() != nil
}

func (l *lifecycle) operationResult(session, kind string, op *sessionOperation, done bool) {
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	value := operationResultSpec{Session: session, Kind: kind, Done: done}
	if done && op.err != nil {
		value.Error = op.err.Error()
	}
	l.runner.fail(publishLifecycle(l, operationResultType, op.id, value))
}

func (l *lifecycle) forgetOperation(op *sessionOperation) {
	if op == nil {
		return
	}
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	err := l.state.Destroy(l.runner.ctx, resource.NewMetadata(lifecycleNamespace, operationResultType, op.id, resource.VersionUndefined), state.WithDestroyOwner("runner-host"))
	if !state.IsNotFoundError(err) {
		l.runner.fail(err)
	}
}

func (s *Session) startOperation(slot **sessionOperation, kind string, work func(context.Context) error) {
	s.mu.Lock()
	if *slot != nil {
		s.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(s.runner.ctx)
	op := &sessionOperation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	*slot = op
	s.work.Add(1)
	s.mu.Unlock()
	s.runner.lifecycle.operationResult(s.id, kind, op, false)
	s.wake()
	go func() {
		defer s.work.Done()
		defer cancel()
		err := work(ctx)
		s.mu.Lock()
		op.err = err
		close(op.done)
		s.mu.Unlock()
		s.runner.lifecycle.operationResult(s.id, kind, op, true)
		s.wake()
		s.runner.changed()
	}()
}

func (s *Session) takeOperation(slot **sessionOperation) (*sessionOperation, bool) {
	s.mu.Lock()
	if !operationDone(*slot) {
		s.mu.Unlock()
		return nil, false
	}
	op := *slot
	*slot = nil
	s.mu.Unlock()
	s.wake()
	return op, true
}

type sessionLifecycleController struct{ l *lifecycle }

func (*sessionLifecycleController) Name() string { return sessionControllerName }
func (*sessionLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: sessionInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: bootstrapRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: codexRuntimeType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: sessionRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(16)),
	}
}

func (c *sessionLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	if ptr.Type() == operationResultType {
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, operationResultType, ptr.ID())
		if err != nil || !found || result.Session == "" {
			return nil, err
		}
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, sessionInputType, result.Session, resource.VersionUndefined)}, nil
	}
	rows, err := r.List(ctx, resource.NewMetadata(lifecycleNamespace, sessionInputType, "", resource.VersionUndefined))
	if err != nil {
		return nil, err
	}
	result := make([]resource.Pointer, 0, len(rows.Items))
	for _, row := range rows.Items {
		result = append(result, row.Metadata())
	}
	return result, nil
}

func (c *sessionLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	c.l.runner.mu.Lock()
	s := c.l.runner.sessions[ptr.ID()]
	c.l.runner.mu.Unlock()
	if s == nil {
		return nil
	}
	input, found, err := lifecycleRead[sessionInputSpec](ctx, r, sessionInputType, s.id)
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, sessionRuntimeType, s.id)
	if err != nil {
		return err
	}
	goal, _, err := lifecycleRead[runnerGoalSpec](ctx, r, runnerGoalType, "runner")
	if err != nil {
		return err
	}
	outer, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, runnerRuntimeType, "runner")
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, sessionRuntimeType, s.id, current)
		if changed {
			s.runner.changed()
		}
		return err
	}
	if goal.Mode == runnerRunning && current.Generation != goal.Generation {
		for _, slot := range []**sessionOperation{&s.halting, &s.sealing} {
			if op, done := s.takeOperation(slot); done {
				c.l.forgetOperation(op)
			}
		}
		current = sessionRuntimeSpec{Generation: goal.Generation, SetupAttempt: current.SetupAttempt}
	} else if current.Generation != goal.Generation {
		current.Generation = goal.Generation
		current.Error = ""
	}
	if op, done := s.takeOperation(&s.setupJob); done {
		c.l.forgetOperation(op)
		if current.SetupAttempt == "" {
			current.SetupAttempt = op.id
		}
		if op.err != nil {
			current.Error = op.err.Error()
			s.runner.fail(op.err)
			if err := write(); err != nil {
				return err
			}
			return op.err
		}
	}
	if input.SetupRequested && current.SetupAttempt == "" {
		s.mu.Lock()
		script, job, setup := s.setupScript, s.setupJob, s.setup
		s.mu.Unlock()
		if script != nil && job == nil && setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
			s.startOperation(&s.setupJob, "setup", func(ctx context.Context) error { return s.runSetup(ctx, *script) })
			s.mu.Lock()
			current.SetupAttempt = s.setupJob.id
			s.mu.Unlock()
			if err := write(); err != nil {
				return err
			}
		}
	}
	if op, done := s.takeOperation(&s.preparation); done {
		c.l.forgetOperation(op)
		s.mu.Lock()
		processID := s.processID
		live := s.process != nil && s.process.Running()
		s.mu.Unlock()
		if op.err == nil && live && goal.Mode == runnerRunning && current.Phase == executionPreparing {
			current.Phase, current.ProcessID = executionReady, processID
			s.mu.Lock()
			s.processState = runnerpb.ProcessState_PROCESS_STATE_RUNNING
			s.mu.Unlock()
		} else if goal.Mode == runnerRunning {
			current.Phase = executionDormant
			s.mu.Lock()
			pending := len(s.pending)
			s.mu.Unlock()
			if pending == 0 {
				current.Attempt = input.Demand
			}
		}
	}
	if op, done := s.takeOperation(&s.dispatching); done {
		c.l.forgetOperation(op)
		if op.err != nil {
			return op.err
		}
	}
	if op, done := s.takeOperation(&s.inspection); done {
		c.l.forgetOperation(op)
		if op.err != nil {
			return op.err
		}
	}
	// Reconciliation runs independently from halt, so native stdout can deliver its replies.
	s.mu.Lock()
	inspectionPending := len(s.reconciliations) > 0
	if s.inspection == nil && inspectionPending && s.reconciliations[0].ready {
		pending := s.reconciliations[0]
		s.reconciliations = s.reconciliations[1:]
		s.mu.Unlock()
		s.startOperation(&s.inspection, "inspect", func(ctx context.Context) error {
			report, err := pending.adapter.Reconcile(ctx, pending.turnID, false)
			if err == nil {
				err = s.Emit(ctx, observation(report), pending.sources)
			}
			if err != nil {
				s.mu.Lock()
				s.reconciliations = append([]*pendingReconciliation{pending}, s.reconciliations...)
				s.mu.Unlock()
			}
			return err
		})
	} else {
		s.mu.Unlock()
	}
	if current.Phase != executionHalted {
		if err := s.dispatchControl(ctx); err != nil {
			return err
		}
	}
	if goal.Mode != runnerRunning {
		if outer.Generation != goal.Generation || outer.Stage < stageHalting {
			return write()
		}
		if current.Phase != executionHalted {
			current.Phase = executionStopping
			if err := write(); err != nil {
				return err
			}
			s.mu.Lock()
			halt := s.halting
			s.mu.Unlock()
			if halt == nil {
				reason := stopClose
				if goal.SealUID != "" {
					reason = stopQuiesce
				}
				s.startOperation(&s.halting, "halt", func(ctx context.Context) error { s.halt(ctx, reason); return nil })
				return nil
			}
			s.mu.Lock()
			settled := operationDone(halt) && len(s.controlDispatches) == 0
			s.mu.Unlock()
			if settled {
				current.Phase = executionHalted
				if err := write(); err != nil {
					return err
				}
			}
		}
		if op, done := s.takeOperation(&s.stopping); done {
			c.l.forgetOperation(op)
			if op.err != nil {
				current.Error = op.err.Error()
				if err := write(); err != nil {
					return err
				}
				return op.err
			}
			current.ProcessStopped = true
		}
		if current.Phase == executionHalted && outer.Stage >= stageStopping && !current.ProcessStopped {
			s.mu.Lock()
			shared := s.runner.codex != nil && s.record.Harness == runnerpb.Harness_HARNESS_CODEX
			s.mu.Unlock()
			if shared {
				status, found, err := lifecycleRead[codexRuntimeSpec](ctx, r, codexRuntimeType, "codex")
				if err != nil {
					return err
				}
				if found && status.Phase == codexStopped {
					current.ProcessStopped = true
				}
			} else {
				s.startOperation(&s.stopping, "stop-process", s.stopProcess)
			}
		}
		if op, done := s.takeOperation(&s.sealing); done {
			c.l.forgetOperation(op)
			if op.err != nil {
				return op.err
			}
		}
		s.mu.Lock()
		inspected := s.inspection == nil && len(s.reconciliations) == 0
		s.mu.Unlock()
		if current.Phase == executionHalted && current.ProcessStopped && inspected && outer.Stage >= stageSealing && goal.SealUID != "" && s.journal.SealState() == nil {
			s.startOperation(&s.sealing, "seal", func(ctx context.Context) error {
				if err := s.sealJournal(ctx); err != nil {
					return err
				}
				return c.l.publishSeal(s)
			})
		}
		return write()
	}
	s.mu.Lock()
	processLive := s.process != nil && s.process.Running()
	processID := s.processID
	s.mu.Unlock()
	if current.Phase == executionReady && (!processLive || processID != current.ProcessID) {
		current.Phase = executionDormant
	}
	s.mu.Lock()
	inspecting := s.inspection != nil || len(s.reconciliations) > 0
	s.mu.Unlock()
	if current.Phase == executionDormant && !inspecting && input.Demand > current.Attempt && !s.executionClosed() {
		started, err := s.journal.HasStartedTurn(ctx)
		if err != nil {
			return err
		}
		s.mu.Lock()
		resuming := s.record.NativeSessionID != nil && started
		s.mu.Unlock()
		current.Phase, current.Attempt, current.Resuming = executionPreparing, input.Demand, resuming
		current.ProcessStopped = false
		if err := write(); err != nil {
			return err
		}
		s.startOperation(&s.preparation, "prepare", func(ctx context.Context) error {
			err := s.ensureReady(ctx)
			if err == nil {
				s.mu.Lock()
				live := s.process != nil && s.process.Running()
				s.mu.Unlock()
				if !live {
					err = errors.New("harness exited before preparation completed")
				}
			}
			if err != nil && !errors.Is(err, context.Canceled) {
				if failure := s.settleUndispatched(s.runner.ctx, "harness startup failed before this command was dispatched: "+err.Error()); failure != nil {
					s.runner.fail(failure)
					return failure
				}
				if failure := s.Emit(s.runner.ctx, observation(&eventpb.HarnessLaunchFailed{Reason: err.Error()}), nil); failure != nil {
					s.runner.fail(failure)
					return failure
				}
			}
			return err
		})
		return nil
	}
	if err := write(); err != nil {
		return err
	}
	s.mu.Lock()
	blocked := s.inspection != nil || len(s.reconciliations) > 0 || s.dispatching != nil
	adapter := s.adapter
	s.mu.Unlock()
	if current.Phase != executionReady || blocked || adapter == nil || adapter.BlocksCommandDispatch() {
		return nil
	}
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	var dispatch []*commandpb.Command
	for _, command := range commands {
		if !s.dispatched[command.CommandId] {
			dispatch = append(dispatch, command)
		}
	}
	s.mu.Unlock()
	if len(dispatch) > 0 {
		s.startOperation(&s.dispatching, "dispatch", func(ctx context.Context) error {
			for _, command := range dispatch {
				if err := s.dispatch(ctx, command); err != nil {
					return err
				}
				if adapter.BlocksCommandDispatch() {
					break
				}
			}
			return nil
		})
	}
	return nil
}

type runnerLifecycleController struct{ l *lifecycle }

func (*runnerLifecycleController) Name() string { return runnerControllerName }
func (*runnerLifecycleController) Inputs() []controller.Input {
	var inputs []controller.Input
	for _, kind := range []string{runnerGoalType, runnerInputType, sessionInputType, sessionRuntimeType, operationResultType, journalSealType, ingressAckType, executorRuntimeType, codexRuntimeType, bootstrapRuntimeType} {
		inputs = append(inputs, controller.Input{Namespace: lifecycleNamespace, Type: kind, Kind: controller.InputWeak})
	}
	return inputs
}

func (*runnerLifecycleController) Outputs() []controller.Output {
	return []controller.Output{{Type: runnerRuntimeType, Kind: controller.OutputExclusive}}
}

func (c *runnerLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}
		if err := c.reconcile(ctx, r); err != nil {
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *runnerLifecycleController) reconcile(ctx context.Context, r controller.Runtime) error {
	goal, _, err := lifecycleRead[runnerGoalSpec](ctx, r, runnerGoalType, "runner")
	if err != nil {
		return err
	}
	current, _, err := lifecycleRead[runnerRuntimeSpec](ctx, r, runnerRuntimeType, "runner")
	if err != nil {
		return err
	}
	if current.Generation != goal.Generation {
		current = runnerRuntimeSpec{Generation: goal.Generation, Stage: stageDraining}
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, runnerRuntimeType, "runner", current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	if goal.Mode == runnerRunning {
		current.Stage = stageRunning
		return write()
	}
	c.l.runner.mu.Lock()
	admissions := c.l.runner.admissions
	c.l.runner.mu.Unlock()
	if admissions != 0 {
		current.Stage = stageDraining
		return write()
	}
	ids, err := c.l.runner.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := c.l.runner.load(ctx, id); err != nil {
			return err
		}
	}
	if current.Stage < stageHalting {
		current.Stage = stageHalting
		return write()
	}
	sessions := c.l.runner.inventory()
	for _, s := range sessions {
		runtime, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, sessionRuntimeType, s.id)
		if err != nil {
			return err
		}
		if runtime.Phase != executionHalted {
			return write()
		}
	}
	if current.Stage < stageStopping {
		current.Stage = stageStopping
		if err := write(); err != nil {
			return err
		}
	}
	for _, s := range sessions {
		runtime, _, err := lifecycleRead[sessionRuntimeSpec](ctx, r, sessionRuntimeType, s.id)
		if err != nil {
			return err
		}
		if runtime.Error != "" {
			current.Error = runtime.Error
			return write()
		}
		if !runtime.ProcessStopped {
			return write()
		}
	}
	if c.l.runner.codex != nil {
		runtime, found, err := lifecycleRead[codexRuntimeSpec](ctx, r, codexRuntimeType, "codex")
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			return write()
		}
		if !found || runtime.Phase != codexStopped {
			return write()
		}
	}
	if current.Stage < stageFencing {
		current.Stage = stageFencing
		return write()
	}
	if c.l.runner.executor != nil {
		runtime, found, err := lifecycleRead[executorRuntimeSpec](ctx, r, executorRuntimeType, "executor")
		if err != nil {
			return err
		}
		if runtime.StopError != "" {
			current.Error = runtime.StopError
			return write()
		}
		if !found || runtime.Phase != executorStopped {
			return write()
		}
	}
	if !c.l.runner.bootstrapSettled() {
		return write()
	}
	// Process exit can finish an outstanding inspection. Keep its journal and
	// controller alive until the resulting observations have been committed.
	for _, s := range sessions {
		input, _, err := lifecycleRead[sessionInputSpec](ctx, r, sessionInputType, s.id)
		if err != nil {
			return err
		}
		if input.Inspections != 0 || input.InspectionRunning {
			return write()
		}
	}
	if goal.SealUID != "" {
		if current.Stage < stageSealing {
			current.Stage = stageSealing
			return write()
		}
		for _, s := range sessions {
			seal, _, err := lifecycleRead[journalSealSpec](ctx, r, journalSealType, s.id)
			if err != nil {
				return err
			}
			if !seal.Present || seal.IncarnationUID != goal.SealUID {
				return write()
			}
		}
	}
	if goal.Mode == runnerClosing {
		// Closing the final admission window and publishing completion share
		// the RPC barrier, including late exact-turn controls.
		c.l.runner.mu.Lock()
		if c.l.runner.admissions != 0 {
			c.l.runner.mu.Unlock()
			return nil
		}
		latest := c.l.goal()
		if latest != goal {
			c.l.runner.mu.Unlock()
			return nil
		}
		current.Stage = stageClosed
		changed, err := lifecycleWrite(ctx, r, runnerRuntimeType, "runner", current)
		c.l.runner.mu.Unlock()
		if changed {
			c.l.runner.changed()
		}
		return err
	}
	current.Stage = stageFlushing
	for _, s := range sessions {
		seal, _, err := lifecycleRead[journalSealSpec](ctx, r, journalSealType, s.id)
		if err != nil {
			return err
		}
		ack, found, err := lifecycleRead[ingressAckSpec](ctx, r, ingressAckType, s.id)
		if err != nil {
			return err
		}
		if !found || !ack.Sealed || ack.Cursor != seal.Cursor {
			return write()
		}
	}
	current.Stage = stageQuiesced
	return write()
}

func (l *lifecycle) status() runnerRuntimeSpec {
	value, _, err := lifecycleRead[runnerRuntimeSpec](l.runner.ctx, l.state, runnerRuntimeType, "runner")
	if err != nil {
		l.runner.fail(err)
	}
	return value
}

func (l *lifecycle) wait(ctx context.Context, minimum runnerStage) error {
	for {
		changed := l.runner.changesSince()
		current := l.status()
		if current.Error != "" {
			return errors.New(current.Error)
		}
		if current.Generation == l.goal().Generation && current.Stage >= minimum {
			return nil
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}
