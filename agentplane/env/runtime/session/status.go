package session

import (
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
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
	id                string
	spec              *sessionpb.SessionSpec
	cursor            uint64
	activeTurn        string
	nativeActiveTurn  bool
	setup             sessionpb.SetupState
	launch            sessionpb.LaunchOutcome
	incarnation       *string
	harnessFailure    *uint64
	processID         string
	processState      sessionpb.ProcessState
	executionIdentity *identitypb.Identity
	pid               *int64
	shared            bool
	ready             bool
	preparing         bool
	pending           uint32
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
		harnessFailure: copied(s.harnessFailure),
		processID:      s.processID, processState: s.processState, shared: s.codexAppServer != nil,
		ready:     !s.halting.Finished() && (current.Phase == Ready || current.Phase == Stopping) && current.ProcessID == s.processID && live,
		preparing: current.Phase == Preparing, pending: uint32(len(s.pending)),
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
	return &sessionpb.Attached{SessionId: o.id, Spec: proto.Clone(o.spec).(*sessionpb.SessionSpec), LastCursor: o.cursor, HarnessState: harness, ActiveTurnId: o.activeTurn, SetupState: o.setup, LaunchOutcome: o.launch, IncarnationUid: copied(o.incarnation), ExecutionIdentity: o.executionIdentity}
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
	if o.pending > 0 {
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

func (o Observation) ProcessStatus() *sessionpb.HarnessProcessStatus {
	if o.processID == "" || o.shared {
		return nil
	}
	return &sessionpb.HarnessProcessStatus{ProcessId: o.processID, Harness: o.spec.GetPlacement().GetEnvironmentHosted().GetHarness(), State: o.processState, AssignedSessionIds: []string{o.id}, Pid: copied(o.pid)}
}

func (s *Session) Attached() *sessionpb.Attached { return s.Observe().Attached() }

func (o Observation) HarnessHostSession() *harnesshostpb.Session {
	attachment := harnesshostpb.HarnessAttachment_STATE_DETACHED
	if o.ready {
		attachment = harnesshostpb.HarnessAttachment_STATE_ATTACHED
	} else if o.preparing || o.processID != "" {
		attachment = harnesshostpb.HarnessAttachment_STATE_STARTING
	}
	return &harnesshostpb.Session{
		SessionId: o.id, ActiveTurnId: o.activeTurn, PendingCommands: o.pending,
		Host: &harnesshostpb.Session_EnvironmentHosted{EnvironmentHosted: &harnesshostpb.EnvironmentHostedSession{
			Setup:   o.setup,
			Harness: &harnesshostpb.HarnessAttachment{State: attachment, FailureCursor: copied(o.harnessFailure)},
		}},
	}
}

func (s *Session) AutosuspendBlockers() []*environmentlifecyclepb.AutosuspendBlocker {
	return s.Observe().AutosuspendBlockers()
}

func (s *Session) ProcessStatus() *sessionpb.HarnessProcessStatus {
	return s.Observe().ProcessStatus()
}

// blockerNotification remembers only which autosuspend predicates have already
// signaled the Runtime's observers. Status and admission always read the authorities.
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
