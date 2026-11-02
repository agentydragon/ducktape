package harnesshost

import (
	"context"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/session"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
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
	if desiredState := r.lifecycle.desiredState(); desiredState.Mode != harnesshostlifecycle.Running {
		minimum := harnesshostlifecycle.Flushing
		if desiredState.Mode == harnesshostlifecycle.Closing {
			minimum = harnesshostlifecycle.Closed
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
	desired, desiredFound, err := harnesshostlifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil {
		return nil, rpcError(err)
	}
	progress, progressFound, err := harnesshostlifecycle.ReadStatus(ctx, r.lifecycle.state)
	if err != nil {
		return nil, rpcError(err)
	}
	result.Lifecycle = harnessHostLifecycleStatus(desired, desiredFound, progress, progressFound)
	return result, nil
}

func harnessHostLifecycleStatus(desired harnesshostlifecycle.Desired, desiredFound bool, progress harnesshostlifecycle.Status, progressFound bool) *sessionpb.EnvironmentRuntimeLifecycleStatus {
	current := desiredFound && progressFound && desired.Generation == progress.Generation
	status := &sessionpb.EnvironmentRuntimeLifecycleStatus{
		DesiredMode:        harnessHostLifecycleMode(desired.Mode, desiredFound),
		DesiredGeneration:  desired.Generation,
		ProgressGeneration: progress.Generation,
		ProgressAvailable:  progressFound,
		ProgressCurrent:    current,
		Stage:              harnessHostLifecycleStage(progress.Stage, progressFound),
	}
	if current {
		status.Error = progress.Error
		if progress.Blocker.Kind != harnesshostlifecycle.NoBlocker {
			status.Blocker = harnessHostLifecycleBlocker(progress.Blocker)
		}
	}
	return status
}

func harnessHostLifecycleMode(mode harnesshostlifecycle.Mode, found bool) sessionpb.EnvironmentRuntimeLifecycleMode {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
	switch mode {
	case harnesshostlifecycle.Running:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
	case harnesshostlifecycle.Quiescing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING
	case harnesshostlifecycle.Closing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_CLOSING
	default:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
}

func harnessHostLifecycleStage(stage harnesshostlifecycle.Stage, found bool) sessionpb.EnvironmentRuntimeLifecycleStage {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
	switch stage {
	case harnesshostlifecycle.StageRunning:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
	case harnesshostlifecycle.Draining:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_DRAINING
	case harnesshostlifecycle.Halting:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING
	case harnesshostlifecycle.Stopping:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_STOPPING
	case harnesshostlifecycle.Fencing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FENCING
	case harnesshostlifecycle.Sealing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_SEALING
	case harnesshostlifecycle.Flushing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING
	case harnesshostlifecycle.Quiesced:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED
	case harnesshostlifecycle.Closed:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_CLOSED
	default:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
}

func harnessHostLifecycleBlocker(blocker harnesshostlifecycle.Blocker) *sessionpb.EnvironmentRuntimeLifecycleBlocker {
	kind := sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED
	switch blocker.Kind {
	case harnesshostlifecycle.AdmissionsPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_ADMISSIONS
	case harnesshostlifecycle.SessionHaltPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT
	case harnesshostlifecycle.SessionProcessPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS
	case harnesshostlifecycle.CodexAppServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER
	case harnesshostlifecycle.CodexExecServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER
	case harnesshostlifecycle.BootstrapPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP
	case harnesshostlifecycle.ProcessCleanupPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP
	case harnesshostlifecycle.SessionInspectionPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION
	case harnesshostlifecycle.JournalSealPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL
	case harnesshostlifecycle.IngressAcknowledgementPending:
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
	return r.factsWithContext(r.ctx)
}

func (r *Runner) factsWithContext(ctx context.Context) (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
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
	admissions, err := r.lifecycle.admissionCount(ctx)
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
		current, _, err := harnesshostlifecycle.ReadStatus(ctx, r.lifecycle.state)
		if err != nil {
			return nil, err
		}
		desiredState, _, err := harnesshostlifecycle.ReadDesired(ctx, r.lifecycle.state)
		if err != nil {
			return nil, err
		}
		if current.Generation == desiredState.Generation && current.Stage >= harnesshostlifecycle.Flushing {
			facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING
			if current.Stage == harnesshostlifecycle.Quiesced {
				facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED
			}
		}
	}
	return facts, nil
}
