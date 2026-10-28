package runner

import (
	"context"
	"fmt"
	"sort"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
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
	for _, s := range r.sessions.Inventory() {
		a := s.Attached()
		result.Sessions = append(result.Sessions, &runnerpb.SessionSummary{SessionId: a.SessionId, Spec: a.Spec, LastCursor: a.LastCursor, HarnessState: a.HarnessState, ActiveTurnId: a.ActiveTurnId, SetupState: a.SetupState, LaunchOutcome: a.LaunchOutcome})
	}
	return result, nil
}

func (r *Runner) GetStatus(_ context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
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
	return result, nil
}

func (r *Runner) facts() (*controlpb.RunnerFacts, error) {
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
	facts := &controlpb.RunnerFacts{SetupsRunning: setup}
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
				facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_RUNNING
				blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendBootstrap})
			} else {
				switch runtime.Phase {
				case bootstrap.Succeeded:
					facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_SUCCEEDED
				case bootstrap.Failed:
					facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_FAILED
				case bootstrap.Interrupted:
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
