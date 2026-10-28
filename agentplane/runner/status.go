package runner

import (
	"context"
	"fmt"
	"sort"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	autosuspendBootstrap = "Bootstrap"
	autosuspendAdmission = "Admission"
)

func (r *Runner) ListSessions(ctx context.Context, _ *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	if desiredState := r.lifecycle.desiredState(); desiredState.Mode != runnerlifecycle.Running {
		minimum := runnerlifecycle.Flushing
		if desiredState.Mode == runnerlifecycle.Closing {
			minimum = runnerlifecycle.Closed
		}
		if err := r.lifecycle.wait(ctx, minimum); err != nil {
			return nil, rpcError(err)
		}
	}
	result := &runnerpb.ListSessionsResponse{}
	for _, s := range r.inventory() {
		a := s.attached()
		result.Sessions = append(result.Sessions, &runnerpb.SessionSummary{SessionId: a.SessionId, Spec: a.Spec, LastCursor: a.LastCursor, HarnessState: a.HarnessState, ActiveTurnId: a.ActiveTurnId, SetupState: a.SetupState, LaunchOutcome: a.LaunchOutcome})
	}
	return result, nil
}

func (r *Runner) GetStatus(_ context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	result := &runnerpb.RunnerStatus{RunnerBootId: r.bootID}
	for _, s := range r.inventory() {
		s.mu.Lock()
		execution := runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT
		ready := s.readyLocked()
		preparing := s.runtimeState().Phase == executionPreparing
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
		if s.processID != "" && s.codexAppServer == nil {
			process := &runnerpb.HarnessProcessStatus{ProcessId: s.processID, Harness: s.record.Harness, State: s.processState, AssignedSessionIds: []string{s.id}}
			if s.process != nil && s.process.Running() {
				pid := int64(s.process.PID())
				process.Pid = &pid
			}
			result.Processes = append(result.Processes, process)
		}
		result.Sessions = append(result.Sessions, row)
		s.mu.Unlock()
	}
	if r.codexAppServer != nil {
		if process := r.codexAppServer.status(); process != nil {
			result.Processes = append(result.Processes, process)
		}
	}
	return result, nil
}

func (r *Runner) facts() (*controlpb.RunnerFacts, error) {
	sessions := r.inventory()
	var blockers []*sandboxpb.AutosuspendBlocker
	setup := false
	for _, s := range sessions {
		sessionBlockers := s.autosuspendBlockers()
		for _, blocker := range sessionBlockers {
			setup = setup || blocker.GetReason() == autosuspendSetup
		}
		blockers = append(blockers, sessionBlockers...)
	}
	facts := &controlpb.RunnerFacts{SetupsRunning: setup}
	if r.config.Launch.Bootstrap != nil {
		input, requested, err := lifecycleRead[bootstrapInputSpec](r.ctx, r.lifecycle.state, "bootstrap")
		if err != nil {
			return nil, err
		}
		if requested && input.Requested {
			runtime, hasRuntime, err := lifecycleRead[bootstrapRuntimeSpec](r.ctx, r.lifecycle.state, "bootstrap")
			if err != nil {
				return nil, err
			}
			if !hasRuntime || runtime.Phase == bootstrapDormant || runtime.Phase == bootstrapRunning {
				facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_RUNNING
				blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendBootstrap})
			} else {
				switch runtime.Phase {
				case bootstrapSucceeded:
					facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_SUCCEEDED
				case bootstrapFailed:
					facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_FAILED
				case bootstrapInterrupted:
					facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_INTERRUPTED
				default:
					return nil, fmt.Errorf("unknown bootstrap phase %d", runtime.Phase)
				}
			}
		}
	}
	r.mu.Lock()
	if r.admissions > 0 {
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendAdmission})
	}
	busy := len(blockers) > 0
	if busy {
		r.idleSince = nil
	} else if r.idleSince == nil {
		r.idleSince = timestamppb.Now()
	}
	if r.idleSince != nil {
		facts.IdleSince = proto.Clone(r.idleSince).(*timestamppb.Timestamp)
	}
	r.mu.Unlock()
	sort.Slice(blockers, func(i, j int) bool {
		leftSession, rightSession := blockers[i].SessionId, blockers[j].SessionId
		if (leftSession != nil) != (rightSession != nil) {
			return leftSession != nil
		}
		if leftSession != nil && *leftSession != *rightSession {
			return *leftSession < *rightSession
		}
		return blockers[i].Reason < blockers[j].Reason
	})
	facts.AutosuspendBlockers = blockers
	if r.store.Lameduck() {
		facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_DRAINING
		current, desiredState := r.lifecycle.status(), r.lifecycle.desiredState()
		if current.Generation == desiredState.Generation && current.Stage >= runnerlifecycle.Flushing {
			facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_FLUSHING
			if current.Stage == runnerlifecycle.Quiesced {
				facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_QUIESCED
			}
		}
	}
	return facts, nil
}

func (s *Session) attached() *runnerpb.Attached {
	state := s.journal.RecoveryState()
	s.mu.Lock()
	defer s.mu.Unlock()
	harness := runnerpb.HarnessState_HARNESS_STATE_STOPPED
	if s.process != nil && s.process.Running() {
		harness = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	}
	uid, known := s.runner.store.IncarnationUID()
	var incarnation *string
	if known {
		incarnation = &uid
	}
	return &runnerpb.Attached{SessionId: s.id, Spec: s.record.Spec(), LastCursor: state.ThroughCursor, HarnessState: harness, ActiveTurnId: state.ActiveTurnID, SetupState: s.setup, LaunchOutcome: s.launch, IncarnationUid: incarnation}
}

func (s *Session) autosuspendBlockers() []*sandboxpb.AutosuspendBlocker {
	preparing := s.runtimeState().Phase == executionPreparing
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
		add(autosuspendSetup)
	}
	s.mu.Unlock()
	return blockers
}
