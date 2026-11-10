package session

import (
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"
)

const AutosuspendSetup = "Setup"

// Observation is a detached projection of one Session. Its methods reuse the same
// journal checkpoint, lifecycle phase and native process observation. It is not a
// transaction across those independently owned authorities.
type Observation struct {
	id                 string
	spec               *sessionpb.SessionSpec
	cursor             uint64
	activeTurn         string
	nativeActiveTurn   bool
	setup              sessionpb.SetupState
	launch             sessionpb.LaunchOutcome
	incarnation        *string
	nativeSession      *string
	failure            *uint64
	processID          string
	processState       sessionpb.ProcessState
	executionPrincipal *identitypb.ServiceAccountIdentity
	executionIdentity  *identitypb.Identity
	pid                *int64
	shared             bool
	ready              bool
	preparing          bool
	pending            bool
}

func copied[T any](value *T) *T {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func (s *Session) Observe() Observation {
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.lifecycleStatus()
	checkpoint := s.journal.RecoveryState()
	live := s.process != nil && s.process.Running()
	result := Observation{
		id: s.id, spec: s.sessionSpec(checkpoint), cursor: checkpoint.ThroughCursor,
		activeTurn: checkpoint.ActiveTurnID, nativeActiveTurn: s.activeTurn != "", setup: s.setup, launch: s.launch,
		nativeSession: copied(s.record.NativeSessionId), failure: copied(s.failure),
		processID: s.processID, processState: s.processState, shared: s.codexAppServer != nil,
		ready:     !s.halting.Finished() && (current.Phase == Ready || current.Phase == Stopping) && current.ProcessID == s.processID && live,
		preparing: current.Phase == Preparing, pending: len(s.pending) > 0,
		executionPrincipal: nil,
	}
	if s.record.ExecutionPrincipal != nil {
		result.executionPrincipal = proto.Clone(s.record.ExecutionPrincipal).(*identitypb.ServiceAccountIdentity)
	}
	if s.record.ExecutionIdentity != nil {
		result.executionIdentity = proto.Clone(s.record.ExecutionIdentity).(*identitypb.Identity)
	}
	if live {
		pid := int64(s.process.PID())
		result.pid = &pid
	}
	if uid := s.deps.environmentState().IncarnationUID; uid != "" {
		result.incarnation = &uid
	}
	return result
}

func (o Observation) Attached() *sessionpb.Attached {
	harness := sessionpb.HarnessState_HARNESS_STATE_STOPPED
	if o.pid != nil {
		harness = sessionpb.HarnessState_HARNESS_STATE_RUNNING
	}
	return &sessionpb.Attached{SessionId: o.id, Spec: proto.Clone(o.spec).(*sessionpb.SessionSpec), LastCursor: o.cursor, HarnessState: harness, ActiveTurnId: o.activeTurn, SetupState: o.setup, LaunchOutcome: o.launch, IncarnationUid: copied(o.incarnation), ExecutionPrincipal: o.executionPrincipal, ExecutionIdentity: o.executionIdentity}
}

func (o Observation) AutosuspendBlockers() []*environmentlifecyclepb.AutosuspendBlocker {
	var blockers []*environmentlifecyclepb.AutosuspendBlocker
	add := func(reason string) {
		id := o.id
		blockers = append(blockers, &environmentlifecyclepb.AutosuspendBlocker{Reason: reason, SessionId: &id})
	}
	if o.activeTurn != "" || o.nativeActiveTurn {
		add(autosuspendActiveTurn)
	}
	if o.pending {
		add(autosuspendPendingCommand)
	}
	if o.preparing {
		add(autosuspendActivation)
	}
	if o.setup == sessionpb.SetupState_SETUP_STATE_RUNNING {
		add(AutosuspendSetup)
	}
	return blockers
}

func (o Observation) Status() (*sessionpb.SessionStatus, *sessionpb.HarnessProcessStatus) {
	execution := sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT
	if o.ready {
		execution = sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_READY
	}
	if !o.ready && (o.preparing || o.processID != "" || o.pending) || o.setup == sessionpb.SetupState_SETUP_STATE_RUNNING {
		execution = sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING
	}
	if o.failure != nil {
		execution = sessionpb.SessionExecutionState_SESSION_EXECUTION_STATE_FAILED
	}
	row := &sessionpb.SessionStatus{SessionId: o.id, ExecutionState: execution, NativeSessionId: copied(o.nativeSession), FailureCursor: copied(o.failure), Busy: len(o.AutosuspendBlockers()) > 0}
	if o.activeTurn != "" {
		row.ActiveTurnId = copied(&o.activeTurn)
	}
	if o.processID != "" {
		row.ProcessId = copied(&o.processID)
	}
	if o.processID == "" || o.shared {
		return row, nil
	}
	return row, &sessionpb.HarnessProcessStatus{ProcessId: o.processID, Harness: o.spec.GetPlacement().GetEnvironmentHosted().GetHarness(), State: o.processState, AssignedSessionIds: []string{o.id}, Pid: copied(o.pid)}
}

func (s *Session) Attached() *sessionpb.Attached { return s.Observe().Attached() }
func (s *Session) AutosuspendBlockers() []*environmentlifecyclepb.AutosuspendBlocker {
	return s.Observe().AutosuspendBlockers()
}

func (s *Session) Status() (*sessionpb.SessionStatus, *sessionpb.HarnessProcessStatus) {
	return s.Observe().Status()
}

// blockerNotification remembers only which autosuspend predicates have already
// signaled Runner's observers. Status and admission always read the authorities.
type blockerNotification struct {
	active  bool
	pending bool
	setup   sessionpb.SetupState
}

func (s *Session) notifyBlockers() {
	s.mu.Lock()
	current := blockerNotification{
		active:  s.activeTurn != "" || s.journal.RecoveryState().ActiveTurnID != "",
		pending: len(s.pending) > 0,
		setup:   s.setup,
	}
	changed := current != s.notifiedBlockers
	s.notifiedBlockers = current
	s.mu.Unlock()
	if changed {
		s.deps.changed()
	}
}

// The reservation supplies initial configuration; only committed journal outcomes override it.
func (s *Session) sessionSpec(checkpoint journal.RecoveryState) *sessionpb.SessionSpec {
	spec := retained.ReservationSpec(s.record)
	if checkpoint.Model != nil {
		spec.Model = *checkpoint.Model
	}
	if checkpoint.ReasoningEffort != nil {
		spec.ReasoningEffort = *checkpoint.ReasoningEffort
	}
	return spec
}
