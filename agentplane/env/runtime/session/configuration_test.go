package session

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codex"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"google.golang.org/protobuf/proto"
)

func TestConfigurationBatchPublicationAndRecovery(t *testing.T) {
	for _, rollback := range []bool{false, true} {
		t.Run(fmt.Sprintf("rollback=%t", rollback), func(t *testing.T) {
			ctx, root, _, store, manager := newSessionTestManager(t, nil)
			s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
			reservation, err := store.Read(s.id)
			if err != nil {
				t.Fatal(err)
			}
			reservation.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x07})
			if err := store.Write(s.id, reservation); err != nil {
				t.Fatal(err)
			}
			for _, command := range []*commandpb.Command{
				{CommandId: "model", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "new-model"}}},
				{CommandId: "effort", Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}},
			} {
				if _, _, err := s.journal.Admit(ctx, command, ""); err != nil {
					t.Fatal(err)
				}
				if _, err := s.journal.DispatchPlanned(ctx, command.CommandId); err != nil {
					t.Fatal(err)
				}
			}
			before := s.Attached()
			injected := errors.New("abort after both configuration outcomes")
			err = s.journal.WithBatch(ctx, func(ctx context.Context) error {
				if err := s.ModelChanged(ctx, "model", "new-model", nil); err != nil {
					return err
				}
				if err := s.ReasoningEffortChanged(ctx, "effort", "high", nil); err != nil {
					return err
				}
				if got := s.Attached(); got.Spec.Model != before.Spec.Model || got.Spec.ReasoningEffort != before.Spec.ReasoningEffort || got.LastCursor != before.LastCursor {
					t.Fatalf("uncommitted configuration published: %v", got)
				}
				if got := s.Session(); got.Model != before.Spec.Model || got.ReasoningEffort != before.Spec.ReasoningEffort {
					t.Fatalf("native adapter observed uncommitted configuration: %+v", got)
				}
				if rollback {
					return injected
				}
				return nil
			})
			if rollback && !errors.Is(err, injected) || !rollback && err != nil {
				t.Fatal(err)
			}
			wantModel, wantEffort := "new-model", "high"
			if rollback {
				wantModel, wantEffort = before.Spec.Model, before.Spec.ReasoningEffort
			}
			if got := s.Attached(); got.Spec.Model != wantModel || got.Spec.ReasoningEffort != wantEffort {
				t.Fatalf("configuration after batch: %v", got)
			}
			if err := s.journal.Close(); err != nil {
				t.Fatal(err)
			}
			manager.mu.Lock()
			delete(manager.sessions, s.id)
			manager.mu.Unlock()
			recovered, err := manager.load(ctx, s.id)
			if err != nil {
				t.Fatal(err)
			}
			if got := recovered.Attached(); got.Spec.Model != wantModel || got.Spec.ReasoningEffort != wantEffort {
				t.Fatalf("configuration after restart: %v", got)
			}
			for _, id := range []string{"model", "effort"} {
				receipt, err := recovered.journal.GetCommand(ctx, id)
				if err != nil {
					t.Fatal(err)
				}
				if !receipt.DispatchPlanned || (receipt.TerminalCursor == nil) != rollback {
					t.Fatalf("recovery changed outcome certainty: %+v", receipt)
				}
			}
			retained, err := store.Read(s.id)
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(retained, reservation) {
				t.Fatalf("configuration changed reservation or unknown fields: %v", retained)
			}
		})
	}
}

func TestConcurrentConfigurationOutcomesFollowJournalOrder(t *testing.T) {
	ctx, root, _, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	before := s.Attached()
	const changes = 20
	for i := range changes {
		command := &commandpb.Command{CommandId: fmt.Sprint(i), Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: fmt.Sprint(i)}}}
		if i%2 != 0 {
			command.Operation = &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: fmt.Sprint(i)}}
		}
		if _, _, err := s.journal.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
	}
	start := make(chan struct{})
	var workers sync.WaitGroup
	for i := range changes {
		// The same completion can be delivered concurrently more than once.
		for range 2 {
			workers.Go(func() {
				<-start
				var err error
				if i%2 == 0 {
					err = s.ModelChanged(ctx, fmt.Sprint(i), fmt.Sprint(i), nil)
				} else {
					err = s.ReasoningEffortChanged(ctx, fmt.Sprint(i), fmt.Sprint(i), nil)
				}
				if err != nil {
					t.Error(err)
				}
			})
		}
	}
	close(start)
	workers.Wait()
	entries, err := s.journal.Since(ctx, uint64(changes), changes*2)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != changes {
		t.Fatalf("completion count = %d, want %d", len(entries), changes)
	}
	model, effort := before.Spec.Model, before.Spec.ReasoningEffort
	for _, entry := range entries {
		if change := entry.Event.GetModelChanged(); change != nil {
			if change.PreviousModel != model {
				t.Fatalf("model chain diverged: %v, previous %q", change, model)
			}
			model = change.Model
		} else if change := entry.Event.GetReasoningEffortChanged(); change != nil {
			if change.PreviousEffort != effort {
				t.Fatalf("effort chain diverged: %v, previous %q", change, effort)
			}
			effort = change.Effort
		} else {
			t.Fatalf("unexpected event: %v", entry)
		}
	}
	if got := s.Attached().Spec; got.Model != model || got.ReasoningEffort != effort {
		t.Fatalf("configuration diverged from outcomes: %v", got)
	}
}

func TestCodexRecoveredTurnResponseUsesConfirmedModelWithinBatch(t *testing.T) {
	ctx, root, _, store, manager := newSessionTestManager(t, nil)
	s := reserveTestSession(t, ctx, manager, store, "session", filepath.Join(root, "project"), "")
	for _, command := range []*commandpb.Command{
		{CommandId: "input", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}},
		{CommandId: "model", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "confirmed-model"}}},
	} {
		if _, _, err := s.journal.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
		if _, err := s.journal.DispatchPlanned(ctx, command.CommandId); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.journal.RecordNativeRequest(ctx, "request", harness.CodexTurnStart, []string{"input", "model"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Emit(ctx, observation(&eventpb.Native{Direction: eventpb.Direction_DIRECTION_FROM_HARNESS, Line: `{"id":"request","result":{"turn":{"id":"turn"}}}`}), nil); err != nil {
		t.Fatal(err)
	}
	adapter := codex.New(s)
	if err := s.journal.WithBatch(ctx, adapter.(harness.RecoveryAdapter).Recover); err != nil {
		t.Fatal(err)
	}
	entries, err := s.journal.Since(ctx, 2, 10)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, entry := range entries {
		if started := entry.Event.GetTurnStarted(); started != nil {
			found = true
			if started.Model != "confirmed-model" {
				t.Fatalf("turn published stale model: %v", started)
			}
		}
	}
	if !found || s.Attached().Spec.Model != "confirmed-model" {
		t.Fatalf("confirmed turn absent: %v", entries)
	}
}
