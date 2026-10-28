package session

import (
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

const AutosuspendSetup = "Setup"

func (s *Session) Attached() *runnerpb.Attached {
	state := s.journal.RecoveryState()
	s.mu.Lock()
	defer s.mu.Unlock()
	harness := runnerpb.HarnessState_HARNESS_STATE_STOPPED
	if s.process != nil && s.process.Running() {
		harness = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	}
	uid, known := s.deps.store.IncarnationUID()
	var incarnation *string
	if known {
		incarnation = &uid
	}
	return &runnerpb.Attached{SessionId: s.id, Spec: s.record.Spec(), LastCursor: state.ThroughCursor, HarnessState: harness, ActiveTurnId: state.ActiveTurnID, SetupState: s.setup, LaunchOutcome: s.launch, IncarnationUid: incarnation}
}

func (s *Session) AutosuspendBlockers() []*sandboxpb.AutosuspendBlocker {
	preparing := s.runtimeState().Phase == Preparing
	s.mu.Lock()
	var blockers []*sandboxpb.AutosuspendBlocker
	add := func(reason string) {
		sessionID := s.id
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: reason, SessionId: &sessionID})
	}
	if s.activeTurn != "" {
		add(autosuspendActiveTurn)
	}
	if len(s.pending) > 0 {
		add(autosuspendPendingCommand)
	}
	if preparing {
		add(autosuspendActivation)
	}
	if s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		add(AutosuspendSetup)
	}
	s.mu.Unlock()
	return blockers
}

func (s *Session) Status() (*runnerpb.SessionStatus, *runnerpb.HarnessProcessStatus) {
	s.mu.Lock()
	defer s.mu.Unlock()
	execution := runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT
	ready := s.readyLocked()
	preparing := s.runtimeState().Phase == Preparing
	if ready {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_READY
	}
	if !ready && (preparing || s.processID != "" || len(s.pending) > 0) || s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING
	}
	if s.failure != nil {
		execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_FAILED
	}
	row := &runnerpb.SessionStatus{SessionId: s.id, ExecutionState: execution, NativeSessionId: s.record.NativeSessionID, FailureCursor: s.failure, Busy: s.activeTurn != "" || len(s.pending) > 0 || preparing || s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING}
	if s.activeTurn != "" {
		value := s.activeTurn
		row.ActiveTurnId = &value
	}
	if s.processID != "" {
		id := s.processID
		row.ProcessId = &id
	}
	if s.processID == "" || s.codexAppServer != nil {
		return row, nil
	}
	process := &runnerpb.HarnessProcessStatus{ProcessId: s.processID, Harness: s.record.Harness, State: s.processState, AssignedSessionIds: []string{s.id}}
	if s.process != nil && s.process.Running() {
		pid := int64(s.process.PID())
		process.Pid = &pid
	}
	return row, process
}
