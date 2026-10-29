package runner

import (
	"context"
	"fmt"
	"sort"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	autosuspendBootstrap     = "Bootstrap"
	autosuspendAdmission     = "Admission"
	autosuspendExecutorRelay = "ExecutorRelay"
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
	for _, s := range r.sessions.Inventory() {
		a := s.Attached()
		result.Sessions = append(result.Sessions, &runnerpb.SessionSummary{SessionId: a.SessionId, Spec: a.Spec, LastCursor: a.LastCursor, HarnessState: a.HarnessState, ActiveTurnId: a.ActiveTurnId, SetupState: a.SetupState, LaunchOutcome: a.LaunchOutcome})
	}
	return result, nil
}

func (r *Runner) GetStatus(ctx context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	result := &runnerpb.RunnerStatus{RunnerBootId: r.bootID}
	for _, s := range r.sessions.Inventory() {
		row, process := s.Status()
		result.Sessions = append(result.Sessions, row)
		if process != nil {
			result.Processes = append(result.Processes, process)
		}
	}
	if r.codexAppServer != nil {
		if process := r.codexAppServer.Status(); process != nil {
			result.Processes = append(result.Processes, process)
		}
	}
	desired, desiredFound, err := runnerlifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil {
		return nil, rpcError(err)
	}
	progress, progressFound, err := runnerlifecycle.ReadStatus(ctx, r.lifecycle.state)
	if err != nil {
		return nil, rpcError(err)
	}
	result.Lifecycle = runnerLifecycleStatus(desired, desiredFound, progress, progressFound)
	return result, nil
}

func runnerLifecycleStatus(desired runnerlifecycle.Desired, desiredFound bool, progress runnerlifecycle.Status, progressFound bool) *runnerpb.RunnerLifecycleStatus {
	current := desiredFound && progressFound && desired.Generation == progress.Generation
	status := &runnerpb.RunnerLifecycleStatus{
		DesiredMode:        runnerLifecycleMode(desired.Mode, desiredFound),
		DesiredGeneration:  desired.Generation,
		ProgressGeneration: progress.Generation,
		ProgressAvailable:  progressFound,
		ProgressCurrent:    current,
		Stage:              runnerLifecycleStage(progress.Stage, progressFound),
	}
	if current {
		status.Error = progress.Error
		if progress.Blocker.Kind != runnerlifecycle.NoBlocker {
			status.Blocker = runnerLifecycleBlocker(progress.Blocker)
		}
	}
	return status
}

func runnerLifecycleMode(mode runnerlifecycle.Mode, found bool) runnerpb.RunnerLifecycleMode {
	if !found {
		return runnerpb.RunnerLifecycleMode_RUNNER_LIFECYCLE_MODE_UNSPECIFIED
	}
	switch mode {
	case runnerlifecycle.Running:
		return runnerpb.RunnerLifecycleMode_RUNNER_LIFECYCLE_MODE_RUNNING
	case runnerlifecycle.Quiescing:
		return runnerpb.RunnerLifecycleMode_RUNNER_LIFECYCLE_MODE_QUIESCING
	case runnerlifecycle.Closing:
		return runnerpb.RunnerLifecycleMode_RUNNER_LIFECYCLE_MODE_CLOSING
	default:
		return runnerpb.RunnerLifecycleMode_RUNNER_LIFECYCLE_MODE_UNSPECIFIED
	}
}

func runnerLifecycleStage(stage runnerlifecycle.Stage, found bool) runnerpb.RunnerLifecycleStage {
	if !found {
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_UNSPECIFIED
	}
	switch stage {
	case runnerlifecycle.StageRunning:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_RUNNING
	case runnerlifecycle.Draining:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_DRAINING
	case runnerlifecycle.Halting:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_HALTING
	case runnerlifecycle.Stopping:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_STOPPING
	case runnerlifecycle.Fencing:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_FENCING
	case runnerlifecycle.Sealing:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_SEALING
	case runnerlifecycle.Flushing:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_FLUSHING
	case runnerlifecycle.Quiesced:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_QUIESCED
	case runnerlifecycle.Closed:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_CLOSED
	default:
		return runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_UNSPECIFIED
	}
}

func runnerLifecycleBlocker(blocker runnerlifecycle.Blocker) *runnerpb.RunnerLifecycleBlocker {
	kind := runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED
	switch blocker.Kind {
	case runnerlifecycle.AdmissionsPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_ADMISSIONS
	case runnerlifecycle.SessionHaltPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_HALT
	case runnerlifecycle.SessionProcessPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS
	case runnerlifecycle.CodexAppServerPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER
	case runnerlifecycle.CodexExecServerPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER
	case runnerlifecycle.BootstrapPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP
	case runnerlifecycle.ProcessCleanupPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP
	case runnerlifecycle.SessionInspectionPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION
	case runnerlifecycle.JournalSealPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL
	case runnerlifecycle.IngressAcknowledgementPending:
		kind = runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT
	}
	return &runnerpb.RunnerLifecycleBlocker{
		Kind: kind, Error: blocker.Error, ResourceType: blocker.ResourceType,
		ResourceId: blocker.ResourceID, ProcessId: blocker.ProcessID,
		ResourcePresent: blocker.ResourcePresent, Admissions: blocker.Admissions,
		JournalSealPresent: blocker.JournalSealPresent, JournalSealUid: blocker.JournalSealUID,
		ExpectedJournalSealUid: blocker.ExpectedJournalSealUID, JournalSealCursor: blocker.JournalSealCursor,
		AcknowledgementPresent: blocker.AcknowledgementPresent,
		AcknowledgementSealed:  blocker.AcknowledgementSealed,
		AcknowledgementCursor:  blocker.AcknowledgementCursor, FinalizersPending: blocker.FinalizersPending,
	}
}

func (r *Runner) facts() (*runnerstatepb.RunnerFacts, error) {
	sessions := r.sessions.Inventory()
	var blockers []*sandboxpb.AutosuspendBlocker
	setup := false
	for _, s := range sessions {
		sessionBlockers := s.AutosuspendBlockers()
		for _, blocker := range sessionBlockers {
			setup = setup || blocker.GetReason() == session.AutosuspendSetup
		}
		blockers = append(blockers, sessionBlockers...)
	}
	facts := &runnerstatepb.RunnerFacts{SetupsRunning: setup}
	if r.bootstrapHost.Configured() {
		input, requested, err := bootstrap.ReadInput(r.ctx, r.lifecycle.state)
		if err != nil {
			return nil, err
		}
		if requested && input.Requested {
			runtime, hasRuntime, err := bootstrap.ReadRuntime(r.ctx, r.lifecycle.state)
			if err != nil {
				return nil, err
			}
			if !hasRuntime || runtime.Phase == bootstrap.Dormant || runtime.Phase == bootstrap.Running {
				facts.Bootstrap = runnerstatepb.RunnerFacts_BOOTSTRAP_RUNNING
				blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendBootstrap})
			} else {
				switch runtime.Phase {
				case bootstrap.Succeeded:
					facts.Bootstrap = runnerstatepb.RunnerFacts_BOOTSTRAP_SUCCEEDED
				case bootstrap.Failed:
					facts.Bootstrap = runnerstatepb.RunnerFacts_BOOTSTRAP_FAILED
				case bootstrap.Interrupted:
					facts.Bootstrap = runnerstatepb.RunnerFacts_BOOTSTRAP_INTERRUPTED
				default:
					return nil, fmt.Errorf("unknown bootstrap phase %d", runtime.Phase)
				}
			}
		}
	}
	r.mu.Lock()
	admissions, err := r.lifecycle.admissionCount(r.ctx)
	if err != nil {
		r.mu.Unlock()
		return nil, err
	}
	if admissions > 0 {
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendAdmission})
	}
	if len(r.executorRelays) > 0 {
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendExecutorRelay})
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
		facts.QuiesceStage = runnerstatepb.RunnerFacts_QUIESCE_STAGE_DRAINING
		current, desiredState := r.lifecycle.status(), r.lifecycle.desiredState()
		if current.Generation == desiredState.Generation && current.Stage >= runnerlifecycle.Flushing {
			facts.QuiesceStage = runnerstatepb.RunnerFacts_QUIESCE_STAGE_FLUSHING
			if current.Stage == runnerlifecycle.Quiesced {
				facts.QuiesceStage = runnerstatepb.RunnerFacts_QUIESCE_STAGE_QUIESCED
			}
		}
	}
	return facts, nil
}
