package session

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func cutoffInterrupt(id string) *commandpb.Command {
	return &commandpb.Command{CommandId: id, Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn-1"}}}
}

func TestFinalAdmissionCutoffPreservesReceiptsAndSettlesEarlierInterrupt(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := t.Context()
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn-1"}), nil); err != nil {
		t.Fatal(err)
	}
	prior := cutoffInterrupt("before-cutoff")
	receipt, err := s.Admit(ctx, prior, true)
	if err != nil {
		t.Fatalf("lameduck exact-turn interrupt: %v", err)
	}
	stored, err := s.journal.GetCommand(ctx, prior.CommandId)
	if err != nil || stored == nil || stored.TerminalCursor == nil {
		t.Fatalf("interrupt admitted before cutoff was not settled: record=%+v err=%v", stored, err)
	}
	s.closeAdmission(1)
	if s.cutoff == nil || s.admissionClosed() || s.journal.SealState() != nil {
		t.Fatal("final admission gate did not close before settlement and journal sealing")
	}
	if _, err := s.Admit(ctx, cutoffInterrupt("after-cutoff"), true); status.Code(err) != codes.Aborted {
		t.Fatalf("new exact-turn interrupt after cutoff: %v", err)
	}
	ordinary := &commandpb.Command{CommandId: "ordinary-after-cutoff", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "new"}}}
	if _, err := s.Admit(ctx, ordinary, false); status.Code(err) != codes.Aborted {
		t.Fatalf("new ordinary command after cutoff: %v", err)
	}
	if err := s.sealJournal(ctx); err != nil {
		t.Fatal(err)
	}
	sealed := s.journal.SealState()
	if sealed == nil || sealed.Cursor != s.journal.LastCursor() {
		t.Fatalf("seal and final cursor disagree: seal=%+v cursor=%d", sealed, s.journal.LastCursor())
	}
	if !s.admissionClosed() {
		t.Fatal("settled final admission was not observed")
	}
	if _, err := s.Admit(ctx, cutoffInterrupt("after-seal"), true); !errors.Is(err, journal.ErrSealed) {
		t.Fatalf("new interrupt after seal = %v, want sealed journal error", err)
	}
	duplicate, err := s.Admit(ctx, proto.Clone(prior).(*commandpb.Command), true)
	if err != nil || !proto.Equal(duplicate, receipt) {
		t.Fatalf("duplicate after seal = %+v, err %v; want %+v", duplicate, err, receipt)
	}
	conflict := cutoffInterrupt(prior.CommandId)
	conflict.GetInterruptTurn().TurnId = "other-turn"
	if _, err := s.Admit(ctx, conflict, true); !errors.Is(err, journal.ErrCommandConflict) {
		t.Fatalf("conflicting retry after seal = %v, want conflict", err)
	}
	if s.journal.LastCursor() != sealed.Cursor {
		t.Fatalf("retry appended after seal: cursor %d, seal %+v", s.journal.LastCursor(), sealed)
	}
	for _, id := range []string{"after-cutoff", "ordinary-after-cutoff", "after-seal"} {
		entry, err := s.journal.GetCommand(ctx, id)
		if err != nil || entry != nil {
			t.Fatalf("rejected command %s entered journal: record=%+v err=%v", id, entry, err)
		}
	}
}

func TestSuccessorReopenClearsCutoffOnlyAfterCurrentRevisionAndPublication(t *testing.T) {
	s, _, m := frameTestSession(t, []string{"/bin/cat"})
	ctx := t.Context()
	env := Environment{IncarnationUID: "test-incarnation", Quiesced: true}
	m.environment = func() Environment { return env }
	s.deps.environmentState = func() Environment { return env }
	s.closeAdmission(1)
	if err := s.stopProcess(ctx); err != nil {
		t.Fatal(err)
	}
	if err := s.sealJournal(ctx); err != nil {
		t.Fatal(err)
	}
	sealed := s.journal.SealState()
	if err := m.Reopen(ctx, s); err == nil || s.journal.SealState() == nil || s.cutoff == nil {
		t.Fatalf("same incarnation reopened a closed Session: err=%v seal=%+v", err, s.journal.SealState())
	}
	env = Environment{IncarnationUID: "successor"}
	if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "successor", Generation: 1}); err != nil {
		t.Fatal(err)
	}
	if err := m.Reopen(ctx, s); err == nil || s.journal.SealState() == nil || s.cutoff == nil {
		t.Fatalf("stale generation reopened a closed Session: err=%v seal=%+v", err, s.journal.SealState())
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "stale", Generation: 2}); err != nil {
		t.Fatal(err)
	}
	if err := m.Reopen(ctx, s); err == nil || s.journal.SealState() == nil || s.cutoff == nil {
		t.Fatalf("stale revision reopened a closed Session: err=%v seal=%+v", err, s.journal.SealState())
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "successor", Generation: 3}); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("acknowledgement publication failed")
	directory, err := m.store.Directory(s.id)
	if err != nil {
		t.Fatal(err)
	}
	fault := &failAcknowledgementModifyOnce{State: m.state, failure: failure, fail: true}
	store, err := retained.NewStore(filepath.Dir(directory), fault)
	if err != nil {
		t.Fatal(err)
	}
	m.store = store
	if err := m.Reopen(ctx, s); !errors.Is(err, failure) || s.journal.SealState() != nil || s.cutoff == nil {
		t.Fatalf("failed successor acknowledgement publication opened gate: err=%v seal=%+v", err, s.journal.SealState())
	}
	if _, err := s.Admit(ctx, &commandpb.Command{CommandId: "during-failure", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "blocked"}}}, false); status.Code(err) != codes.Aborted {
		t.Fatalf("failed successor acknowledgement publication admitted command: %v", err)
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "other-successor", Generation: 4}); err != nil {
		t.Fatal(err)
	}
	if err := m.Reopen(ctx, s); err == nil || s.cutoff == nil || s.journal.SealState() != nil {
		t.Fatalf("changed successor revision completed interrupted reopen: err=%v cutoff=%+v", err, s.cutoff)
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, m.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, IncarnationUID: "successor", Generation: 5}); err != nil {
		t.Fatal(err)
	}
	signalOperation := s.deps.signalOperation
	s.deps.signalOperation = func(string) error { return failure }
	if err := m.Reopen(ctx, s); !errors.Is(err, failure) || s.cutoff == nil || s.journal.SealState() != nil {
		t.Fatalf("failed reconcile signal completed successor reopen: err=%v cutoff=%+v", err, s.cutoff)
	}
	s.deps.signalOperation = signalOperation
	if err := m.Reopen(ctx, s); err != nil {
		t.Fatalf("retry completed successor reopen: %v", err)
	}
	if s.cutoff != nil || s.admissionClosed() || s.journal.SealState() != nil {
		t.Fatalf("successful successor did not reopen admission: gate=%v seal=%+v", s.cutoff != nil, s.journal.SealState())
	}
	if _, err := s.Admit(ctx, &commandpb.Command{CommandId: "successor-command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "new"}}}, false); err != nil {
		t.Fatalf("successor command admission: %v", err)
	}
	if sealed.IncarnationUID != "test-incarnation" || s.journal.LastCursor() <= sealed.Cursor {
		t.Fatalf("successor journal did not advance past old seal: old=%+v cursor=%d", sealed, s.journal.LastCursor())
	}
}

func TestInterruptRacingFinalAdmissionCutoff(t *testing.T) {
	s, _, _ := frameTestSession(t, []string{"/bin/cat"})
	ctx := context.Background()
	if err := s.Emit(ctx, observation(&eventpb.TurnStarted{TurnId: "turn-1"}), nil); err != nil {
		t.Fatal(err)
	}
	s.admitMu.Lock()
	result := make(chan error, 1)
	command := cutoffInterrupt("racing-interrupt")
	go func() { _, err := s.Admit(ctx, command, true); result <- err }()
	cutoffDone := make(chan struct{})
	go func() { s.closeAdmission(1); close(cutoffDone) }()
	s.admitMu.Unlock()
	err := <-result
	<-cutoffDone
	if err != nil && status.Code(err) != codes.Aborted {
		t.Fatalf("racing interrupt: %v", err)
	}
	if err := s.sealJournal(ctx); err != nil {
		t.Fatal(err)
	}
	record, lookupErr := s.journal.GetCommand(ctx, command.CommandId)
	if lookupErr != nil {
		t.Fatal(lookupErr)
	}
	if err == nil && (record == nil || record.TerminalCursor == nil) {
		t.Fatalf("accepted interrupt was not settled before seal: %+v", record)
	}
	if err != nil && record != nil {
		t.Fatalf("rejected interrupt was journaled: %+v", record)
	}
	if s.journal.SealState().Cursor != s.journal.LastCursor() {
		t.Fatal("journal advanced after seal")
	}
}
