package bootstrap

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

func TestBootstrapLogPersistsOrderedOutputAndTerminal(t *testing.T) {
	stateDir := t.TempDir()
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	first := bytes.Repeat([]byte("x"), 64*1024+7)
	second := []byte{0, 1, 0xff, '\n'}
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, first); err != nil {
		t.Fatal(err)
	}
	if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDERR, second); err != nil {
		t.Fatal(err)
	}
	if _, err := log.AppendResult(t.Context(), 1, 7); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}

	restored, restoredCloser, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	defer restoredCloser.Close()
	replayed, err := OpenLog(t.Context(), restored)
	if err != nil {
		t.Fatal(err)
	}
	events, err := readEvents(t.Context(), restored)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 3 || events[0].GetSequence() != 1 || events[1].GetSequence() != 2 || events[2].GetSequence() != 3 {
		t.Fatalf("replayed sequence = %v, want [1 2 3]", eventSequences(events))
	}
	if events[0].GetOutput().Stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT || !bytes.Equal(events[0].GetOutput().Data, first) {
		t.Fatalf("first output changed during persistence: stream=%v bytes=%d", events[0].GetOutput().Stream, len(events[0].GetOutput().Data))
	}
	if events[1].GetOutput().Stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDERR || !bytes.Equal(events[1].GetOutput().Data, second) {
		t.Fatalf("second output changed during persistence: %v", events[1].GetOutput())
	}
	if result := events[2].GetResult(); result == nil || !result.Executed || result.ExitCode != 7 {
		t.Fatalf("replayed result = %v, want executed with exit code 7", result)
	}
	if !replayed.Ended() {
		t.Fatal("restored successful result is not terminal")
	}
}

type bootstrapLogReadCountingState struct {
	state.State
	lists int
	gets  int
}

func (s *bootstrapLogReadCountingState) List(ctx context.Context, kind resource.Kind, opts ...state.ListOption) (resource.List, error) {
	s.lists++
	return s.State.List(ctx, kind, opts...)
}

func (s *bootstrapLogReadCountingState) Get(ctx context.Context, pointer resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	s.gets++
	return s.State.Get(ctx, pointer, opts...)
}

func assertBootstrapOutcome(t *testing.T, got error, wantMessage string, wantInterrupted bool) {
	t.Helper()
	if wantInterrupted {
		if got != ErrInterrupted {
			t.Fatalf("bootstrap outcome = %v, want ErrInterrupted", got)
		}
		return
	}
	if wantMessage == "" {
		if got != nil {
			t.Fatalf("bootstrap outcome = %v, want nil", got)
		}
		return
	}
	if got == nil || got.Error() != wantMessage {
		t.Fatalf("bootstrap outcome = %v, want %q", got, wantMessage)
	}
}

func TestBootstrapLogRetainsTerminalOutcomeWithoutRescanning(t *testing.T) {
	for _, tc := range []struct {
		name            string
		append          func(context.Context, *Log) (*sessionpb.InitializeResult, error)
		wantError       string
		wantExitCode    int32
		wantInterrupted bool
	}{
		{
			name: "success",
			append: func(ctx context.Context, log *Log) (*sessionpb.InitializeResult, error) {
				return log.AppendResult(ctx, 1, 0)
			},
			wantExitCode: 0,
		},
		{
			name: "failure",
			append: func(ctx context.Context, log *Log) (*sessionpb.InitializeResult, error) {
				return log.AppendResult(ctx, 1, 7)
			},
			wantError:    failedMessage,
			wantExitCode: 7,
		},
		{
			name: "interrupted",
			append: func(ctx context.Context, log *Log) (*sessionpb.InitializeResult, error) {
				return nil, log.AppendInterrupted(ctx, 1)
			},
			wantInterrupted: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, closer, err := openTestState(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			defer closer.Close()
			counting := &bootstrapLogReadCountingState{State: store}
			log, err := OpenLog(t.Context(), counting)
			if err != nil {
				t.Fatal(err)
			}
			if counting.lists != 1 || counting.gets != 0 {
				t.Fatalf("reads during OpenLog = list %d, get %d, want list 1, get 0", counting.lists, counting.gets)
			}
			result, err := tc.append(t.Context(), log)
			if err != nil {
				t.Fatal(err)
			}
			if result != nil {
				result.ExitCode = 99
			}

			snapshot, err := log.terminalEvent(t.Context())
			if err != nil || snapshot == nil {
				t.Fatalf("terminal event = (%v, %v), want event", snapshot, err)
			}
			if snapshot.GetSequence() != 1 {
				t.Fatalf("terminal sequence = %d, want 1", snapshot.GetSequence())
			}
			if !tc.wantInterrupted && snapshot.GetResult().GetExitCode() != tc.wantExitCode {
				t.Fatalf("retained result exit code = %d, want %d", snapshot.GetResult().GetExitCode(), tc.wantExitCode)
			}
			snapshot.Observation = nil
			assertBootstrapOutcome(t, bootstrapErrorForLog(t.Context(), log), tc.wantError, tc.wantInterrupted)
			if counting.lists != 1 || counting.gets != 0 {
				t.Fatalf("reads after append/outcome lookup = list %d, get %d, want list 1, get 0", counting.lists, counting.gets)
			}

			cancelled, cancel := context.WithCancel(t.Context())
			cancel()
			if err := bootstrapErrorForLog(cancelled, log); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled terminal lookup = %v, want context.Canceled", err)
			}

			reopened, err := OpenLog(t.Context(), counting)
			if err != nil {
				t.Fatal(err)
			}
			assertBootstrapOutcome(t, bootstrapErrorForLog(t.Context(), reopened), tc.wantError, tc.wantInterrupted)
			if counting.lists != 2 || counting.gets != 0 {
				t.Fatalf("reads after replay outcome lookup = list %d, get %d, want list 2, get 0", counting.lists, counting.gets)
			}
			if reopened.LastSequence() != 1 || !reopened.Ended() {
				t.Fatalf("reopened terminal state = sequence %d ended %t, want 1 and true", reopened.LastSequence(), reopened.Ended())
			}
		})
	}
}

func TestBootstrapLogFailsClosedAfterUncertainTerminalWrite(t *testing.T) {
	store, closer, err := openTestState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer closer.Close()
	writeErr := errors.New("backing store returned after terminal commit")
	uncertain := &uncertainBootstrapEventState{State: store, err: writeErr}
	log, err := OpenLog(t.Context(), uncertain)
	if err != nil {
		t.Fatal(err)
	}
	if result, err := log.AppendResult(t.Context(), 2, 7); result != nil || err == nil {
		t.Fatalf("invalid attempt result append = (%v, %v), want nil and error", result, err)
	}
	if uncertain.eventCreates != 0 || log.LastSequence() != 0 || log.Ended() {
		t.Fatalf("state after invalid attempt = creates %d, sequence %d, ended %t, want 0, 0, false", uncertain.eventCreates, log.LastSequence(), log.Ended())
	}
	if result, err := log.AppendResult(t.Context(), 1, 7); result != nil || !errors.Is(err, writeErr) {
		t.Fatalf("uncertain result append = (%v, %v), want nil and %v", result, err, writeErr)
	}
	if log.Ended() || log.LastSequence() != 0 {
		t.Fatalf("in-memory state after uncertain result = sequence %d ended %t, want 0 and false", log.LastSequence(), log.Ended())
	}
	if event, err := log.terminalEvent(t.Context()); event != nil || err != nil {
		t.Fatalf("terminal lookup after uncertain write = (%v, %v), want nil and nil for absent in-memory terminal", event, err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if err := bootstrapErrorForLog(cancelled, log); err == nil || err.Error() != operationHasNoOutcome {
		t.Fatalf("cancelled lookup without terminal = %v, want %q", err, operationHasNoOutcome)
	}
	if err := log.AppendInterrupted(t.Context(), 1); !errors.Is(err, writeErr) {
		t.Fatalf("append after uncertain result = %v, want wrapped write error", err)
	}
	if uncertain.eventCreates != 1 {
		t.Fatalf("event creates after uncertain result = %d, want 1", uncertain.eventCreates)
	}

	if err := store.Create(t.Context(), uncertain.committed, state.WithCreateOwner(HostOwner)); err != nil {
		t.Fatal(err)
	}
	recovered, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if !recovered.Ended() || recovered.LastSequence() != 1 {
		t.Fatalf("recovered uncertain log = sequence %d ended %t, want 1 and true", recovered.LastSequence(), recovered.Ended())
	}
	if err := bootstrapErrorForLog(t.Context(), recovered); err == nil || err.Error() != failedMessage {
		t.Fatalf("recovered terminal error = %v, want %q", err, failedMessage)
	}
}

func TestBootstrapLogOrdersReplayBySequenceNotListOrder(t *testing.T) {
	store, closer, err := openTestState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer closer.Close()
	for _, sequence := range []uint64{2, 1} {
		event := &sessionpb.InitializationEvent{
			Sequence: sequence, Attempt: 1,
			Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{
				Stream: sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte{byte('0' + sequence)},
			}},
		}
		if err := store.Create(t.Context(), EventResourceValue(event), state.WithCreateOwner(HostOwner)); err != nil {
			t.Fatal(err)
		}
	}
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if log.LastSequence() != 2 {
		t.Fatalf("replayed last sequence = %d, want 2", log.LastSequence())
	}
	events, err := readEvents(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(eventSequences(events)) != "[1 2]" || string(events[0].GetOutput().Data) != "1" || string(events[1].GetOutput().Data) != "2" {
		t.Fatalf("replay order = %v", events)
	}
}

func TestBootstrapLogRejectsSequenceGapsAndEventsAfterTerminal(t *testing.T) {
	for _, tc := range []struct {
		name   string
		events []*sessionpb.InitializationEvent
	}{
		{
			name: "gap",
			events: []*sessionpb.InitializationEvent{{
				Sequence: 2, Attempt: 1,
				Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{Stream: sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte("late")}},
			}},
		},
		{
			name: "event after terminal",
			events: []*sessionpb.InitializationEvent{
				{Sequence: 1, Attempt: 1, Observation: &sessionpb.InitializationEvent_Interrupted{Interrupted: &sessionpb.InitializationInterrupted{}}},
				{Sequence: 2, Attempt: 1, Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{Stream: sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte("late")}}},
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, closer, err := openTestState(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			defer closer.Close()
			for _, event := range tc.events {
				if err := store.Create(t.Context(), EventResourceValue(event), state.WithCreateOwner(HostOwner)); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := OpenLog(t.Context(), store); err == nil {
				t.Fatal("invalid event sequence was accepted")
			}
		})
	}
}

func TestBootstrapLogConcurrentAppendsRemainDense(t *testing.T) {
	store, closer, err := openTestState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer closer.Close()
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	const count = 64
	writeErr := make(chan error, 1)
	go func() {
		for i := 0; i < count; i++ {
			if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, []byte{byte(i)}); err != nil {
				writeErr <- err
				return
			}
		}
		writeErr <- nil
	}()
	if err := <-writeErr; err != nil {
		t.Fatal(err)
	}
	events, err := readEvents(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != count {
		t.Fatalf("persisted event count = %d, want %d", len(events), count)
	}
	for i, event := range events {
		if event.GetSequence() != uint64(i+1) {
			t.Fatalf("event at index %d has sequence %d, want %d", i, event.GetSequence(), i+1)
		}
		if got := event.GetOutput().GetData(); len(got) != 1 || got[0] != byte(i) {
			t.Fatalf("event %d output = %v", event.GetSequence(), got)
		}
	}
}

type uncertainBootstrapEventState struct {
	state.State
	err          error
	committed    resource.Resource
	eventCreates int
}

func (s *uncertainBootstrapEventState) Create(ctx context.Context, value resource.Resource, opts ...state.CreateOption) error {
	if value.Metadata().Type() == EventType {
		s.eventCreates++
		if s.committed == nil {
			// Model COSI's backing Put succeeding durably before returning an error and
			// before the in-memory collection publishes the resource to ReaderGet/List.
			s.committed = value.DeepCopy()
			if err := s.committed.Metadata().SetOwner(HostOwner); err != nil {
				return err
			}
			return s.err
		}
	}
	return s.State.Create(ctx, value, opts...)
}

func TestBootstrapLogFailsClosedAfterUncertainCommittedWrite(t *testing.T) {
	store, closer, err := openTestState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer closer.Close()
	writeErr := errors.New("backing store returned after commit")
	uncertain := &uncertainBootstrapEventState{State: store, err: writeErr}
	log, err := OpenLog(t.Context(), uncertain)
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, []byte("once")); !errors.Is(err, writeErr) {
		t.Fatalf("output append error = %v, want %v", err, writeErr)
	}
	if err := log.AppendInterrupted(t.Context(), 1); err == nil {
		t.Fatal("append after uncertain write was accepted")
	}
	if uncertain.eventCreates != 1 {
		t.Fatalf("COSI event creates = %d, want 1 after writer fail-closed", uncertain.eventCreates)
	}

	// The event was durably committed but invisible to this process. Simulate startup
	// reloading it from the backing store, then let recovery record interruption at 2.
	if err := store.Create(t.Context(), uncertain.committed, state.WithCreateOwner(HostOwner)); err != nil {
		t.Fatal(err)
	}
	recovered, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if recovered.LastSequence() != 1 || recovered.Ended() {
		t.Fatalf("recovered uncertain log = sequence %d ended %t, want output sequence 1", recovered.LastSequence(), recovered.Ended())
	}
	if err := recovered.AppendInterrupted(t.Context(), 1); err != nil {
		t.Fatalf("record interruption after restart: %v", err)
	}
	events, err := readEvents(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 || string(events[0].GetOutput().Data) != "once" || events[1].GetSequence() != 2 || events[1].GetInterrupted() == nil {
		t.Fatalf("recovered events = %v, want one output followed by interruption", events)
	}
}

func eventSequences(events []*sessionpb.InitializationEvent) []uint64 {
	result := make([]uint64, len(events))
	for i, event := range events {
		result[i] = event.GetSequence()
	}
	return result
}
