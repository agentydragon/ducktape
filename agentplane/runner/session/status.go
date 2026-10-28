package session

import (
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/protobuf/proto"
)

const AutosuspendSetup = "Setup"

// Observation is a detached projection of one Session. Its methods reuse the same
// journal checkpoint, lifecycle phase and native process observation. It is not a
// transaction across those independently owned authorities.
type Observation struct {
	id               string
	spec             *runnerpb.SessionSpec
	cursor           uint64
	activeTurn       string
	nativeActiveTurn bool
	setup            runnerpb.SetupState
	launch           runnerpb.LaunchOutcome
	incarnation      *string
	nativeSession    *string
	failure          *uint64
	processID        string
	processState     runnerpb.ProcessState
	pid              *int64
	shared           bool
	ready            bool
	preparing        bool
	pending          bool
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
	current := s.runtimeState()
	checkpoint := s.journal.RecoveryState()
	live := s.process != nil && s.process.Running()
	result := Observation{
		id: s.id, spec: s.record.Spec(), cursor: checkpoint.ThroughCursor,
		activeTurn: checkpoint.ActiveTurnID, nativeActiveTurn: s.activeTurn != "", setup: s.setup, launch: s.launch,
		nativeSession: copied(s.record.NativeSessionID), failure: copied(s.failure),
		processID: s.processID, processState: s.processState, shared: s.codexAppServer != nil,
		ready:     !operationDone(s.halting) && (current.Phase == Ready || current.Phase == Stopping) && current.ProcessID == s.processID && live,
		preparing: current.Phase == Preparing, pending: len(s.pending) > 0,
	}
	if live {
		pid := int64(s.process.PID())
		result.pid = &pid
	}
	if uid, known := s.deps.store.IncarnationUID(); known {
		result.incarnation = &uid
	}
	return result
}

func (o Observation) Attached() *runnerpb.Attached {
	harness := runnerpb.HarnessState_HARNESS_STATE_STOPPED
	if o.pid != nil {
		harness = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	}
	return &runnerpb.Attached{SessionId: o.id, Spec: proto.Clone(o.spec).(*runnerpb.SessionSpec), LastCursor: o.cursor, HarnessState: harness, ActiveTurnId: o.activeTurn, SetupState: o.setup, LaunchOutcome: o.launch, IncarnationUid: copied(o.incarnation)}
}

func (o Observation) AutosuspendBlockers() []*sandboxpb.AutosuspendBlocker {
	var blockers []*sandboxpb.AutosuspendBlocker
	add := func(reason string) {
		id := o.id
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: reason, SessionId: &id})
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
	if o.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		add(AutosuspendSetup)
	}
	return blockers
}

func (o Observation) Status() (*runnerpb.SessionStatus, *runnerpb.HarnessProcessStatus) {
	execution := runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT
	if o.ready {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_READY
	}
	if !o.ready && (o.preparing || o.processID != "" || o.pending) || o.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING
	}
	if o.failure != nil {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_FAILED
	}
	row := &runnerpb.SessionStatus{SessionId: o.id, ExecutionState: execution, NativeSessionId: copied(o.nativeSession), FailureCursor: copied(o.failure), Busy: len(o.AutosuspendBlockers()) > 0}
	if o.activeTurn != "" {
		row.ActiveTurnId = copied(&o.activeTurn)
	}
	if o.processID != "" {
		row.ProcessId = copied(&o.processID)
	}
	if o.processID == "" || o.shared {
		return row, nil
	}
	return row, &runnerpb.HarnessProcessStatus{ProcessId: o.processID, Harness: o.spec.Harness, State: o.processState, AssignedSessionIds: []string{o.id}, Pid: copied(o.pid)}
}

func (s *Session) Attached() *runnerpb.Attached { return s.Observe().Attached() }
func (s *Session) AutosuspendBlockers() []*sandboxpb.AutosuspendBlocker {
	return s.Observe().AutosuspendBlockers()
}

func (s *Session) Status() (*runnerpb.SessionStatus, *runnerpb.HarnessProcessStatus) {
	return s.Observe().Status()
}
