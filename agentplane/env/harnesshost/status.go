package harnesshost

import (
	"context"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/session"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/lifecycle"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	autosuspendBootstrap = "Bootstrap"
	autosuspendAdmission = "Admission"
	// Keep the stable EnvironmentRuntimeFacts enum value while execution is carried over typed gRPC.
	autosuspendExecution = "ExecutorRelay"
)

func (r *Runner) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	if desiredState := r.lifecycle.desiredState(); desiredState.Mode != runnerlifecycle.Running {
		minimum := runnerlifecycle.Flushing
		if desiredState.Mode == runnerlifecycle.Closing {
			minimum = runnerlifecycle.Closed
		}
		if err := r.lifecycle.wait(ctx, minimum); err != nil {
			return nil, rpcError(err)
		}
	}
	result := &sessionpb.ListSessionsResponse{}
	for _, s := range r.sessions.Inventory() {
		a := s.Attached()
		result.Sessions = append(result.Sessions, &sessionpb.SessionSummary{SessionId: a.SessionId, Spec: a.Spec, LastCursor: a.LastCursor, HarnessState: a.HarnessState, ActiveTurnId: a.ActiveTurnId, SetupState: a.SetupState, LaunchOutcome: a.LaunchOutcome})
	}
	return result, nil
}

func (r *Runner) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	result := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: r.bootID}
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

func runnerLifecycleStatus(desired runnerlifecycle.Desired, desiredFound bool, progress runnerlifecycle.Status, progressFound bool) *sessionpb.EnvironmentRuntimeLifecycleStatus {
	current := desiredFound && progressFound && desired.Generation == progress.Generation
	status := &sessionpb.EnvironmentRuntimeLifecycleStatus{
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

func runnerLifecycleMode(mode runnerlifecycle.Mode, found bool) sessionpb.EnvironmentRuntimeLifecycleMode {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
	switch mode {
	case runnerlifecycle.Running:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
	case runnerlifecycle.Quiescing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING
	case runnerlifecycle.Closing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_CLOSING
	default:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
}

func runnerLifecycleStage(stage runnerlifecycle.Stage, found bool) sessionpb.EnvironmentRuntimeLifecycleStage {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
	switch stage {
	case runnerlifecycle.StageRunning:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
	case runnerlifecycle.Draining:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_DRAINING
	case runnerlifecycle.Halting:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING
	case runnerlifecycle.Stopping:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_STOPPING
	case runnerlifecycle.Fencing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FENCING
	case runnerlifecycle.Sealing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_SEALING
	case runnerlifecycle.Flushing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING
	case runnerlifecycle.Quiesced:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED
	case runnerlifecycle.Closed:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_CLOSED
	default:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
}

func runnerLifecycleBlocker(blocker runnerlifecycle.Blocker) *sessionpb.EnvironmentRuntimeLifecycleBlocker {
	kind := sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED
	switch blocker.Kind {
	case runnerlifecycle.AdmissionsPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_ADMISSIONS
	case runnerlifecycle.SessionHaltPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT
	case runnerlifecycle.SessionProcessPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS
	case runnerlifecycle.CodexAppServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER
	case runnerlifecycle.CodexExecServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER
	case runnerlifecycle.BootstrapPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP
	case runnerlifecycle.ProcessCleanupPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP
	case runnerlifecycle.SessionInspectionPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION
	case runnerlifecycle.JournalSealPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL
	case runnerlifecycle.IngressAcknowledgementPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT
	}
	return &sessionpb.EnvironmentRuntimeLifecycleBlocker{
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

func (r *Runner) facts() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
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
	facts := &environmentruntimestatepb.EnvironmentRuntimeFacts{SetupsRunning: setup}
	r.mu.Lock()
	admissions, err := r.lifecycle.admissionCount(r.ctx)
	if err != nil {
		r.mu.Unlock()
		return nil, err
	}
	if admissions > 0 {
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
	if r.environment().Quiesced {
		facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_DRAINING
		current, desiredState := r.lifecycle.status(), r.lifecycle.desiredState()
		if current.Generation == desiredState.Generation && current.Stage >= runnerlifecycle.Flushing {
			facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING
			if current.Stage == runnerlifecycle.Quiesced {
				facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED
			}
		}
	}
	return facts, nil
}
