package runtime

import (
	"context"
	"sort"

	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
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

func (r *Runtime) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	if err := r.authorizeSessionRead(ctx); err != nil {
		return nil, err
	}
	if desiredState := r.desiredSnapshot(); desiredState.Mode != lifecycle.Running {
		minimum := lifecycle.Flushing
		if desiredState.Mode == lifecycle.Closing {
			minimum = lifecycle.Closed
		}
		if err := r.waitSessionStage(ctx, minimum); err != nil {
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

func (r *Runtime) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if err := r.authorizeSessionRead(ctx); err != nil {
		return nil, err
	}
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
	desired, desiredFound, err := lifecycle.ReadEnvironmentDesired(ctx, r.lifecycleState)
	if err != nil {
		return nil, rpcError(err)
	}
	progress, progressFound, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
	if err != nil {
		return nil, rpcError(err)
	}
	result.Lifecycle = sessionLifecycleStatus(desired, desiredFound, progress, progressFound)
	return result, nil
}

func sessionLifecycleStatus(desired lifecycle.EnvironmentDesired, desiredFound bool, progress lifecycle.Status, progressFound bool) *sessionpb.EnvironmentRuntimeLifecycleStatus {
	current := desiredFound && progressFound && desired.Generation == progress.Generation
	status := &sessionpb.EnvironmentRuntimeLifecycleStatus{
		DesiredMode:        sessionLifecycleMode(desired.Mode, desiredFound),
		DesiredGeneration:  desired.Generation,
		ProgressGeneration: progress.Generation,
		ProgressAvailable:  progressFound,
		ProgressCurrent:    current,
		Stage:              sessionLifecycleStage(progress.Stage, progressFound),
	}
	if current {
		status.Error = progress.Error
		if progress.Blocker.Kind != lifecycle.NoBlocker {
			status.Blocker = sessionLifecycleBlocker(progress.Blocker)
		}
	}
	return status
}

func sessionLifecycleMode(mode lifecycle.Mode, found bool) sessionpb.EnvironmentRuntimeLifecycleMode {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
	switch mode {
	case lifecycle.Running:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING
	case lifecycle.Quiescing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING
	case lifecycle.Closing:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_CLOSING
	default:
		return sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED
	}
}

func sessionLifecycleStage(stage lifecycle.Stage, found bool) sessionpb.EnvironmentRuntimeLifecycleStage {
	if !found {
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
	switch stage {
	case lifecycle.StageRunning:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING
	case lifecycle.Draining:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_DRAINING
	case lifecycle.Halting:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING
	case lifecycle.Stopping:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_STOPPING
	case lifecycle.Fencing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FENCING
	case lifecycle.Sealing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_SEALING
	case lifecycle.Flushing:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING
	case lifecycle.Quiesced:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED
	case lifecycle.Closed:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_CLOSED
	default:
		return sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED
	}
}

func sessionLifecycleBlocker(blocker lifecycle.Blocker) *sessionpb.EnvironmentRuntimeLifecycleBlocker {
	kind := sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED
	switch blocker.Kind {
	case lifecycle.AdmissionsPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_ADMISSIONS
	case lifecycle.SessionHaltPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT
	case lifecycle.SessionProcessPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS
	case lifecycle.CodexAppServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER
	case lifecycle.CodexExecServerPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER
	case lifecycle.BootstrapPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP
	case lifecycle.ProcessCleanupPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP
	case lifecycle.SessionInspectionPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION
	case lifecycle.JournalSealPending:
		kind = sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL
	case lifecycle.IngressAcknowledgementPending:
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

func (r *Runtime) sessionFacts(ctx context.Context) (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
	r.mu.Lock()
	manager := r.sessions
	r.mu.Unlock()
	if manager == nil {
		return &environmentruntimestatepb.EnvironmentRuntimeFacts{}, nil
	}
	sessions := manager.Inventory()
	var blockers []*environmentlifecyclepb.AutosuspendBlocker
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
	admissions, err := r.admissionCount(ctx)
	if err != nil {
		r.mu.Unlock()
		return nil, err
	}
	if admissions > 0 {
		blockers = append(blockers, &environmentlifecyclepb.AutosuspendBlocker{Reason: autosuspendAdmission})
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
		current, _, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
		if err != nil {
			return nil, err
		}
		desiredState, _, err := lifecycle.ReadEnvironmentDesired(ctx, r.lifecycleState)
		if err != nil {
			return nil, err
		}
		if current.Generation == desiredState.Generation && current.Stage >= lifecycle.Flushing {
			facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING
			if current.Stage == lifecycle.Quiesced {
				facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED
			}
		}
	}
	return facts, nil
}

func (r *Runtime) desiredSnapshot() lifecycle.EnvironmentDesired {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.desired
}
