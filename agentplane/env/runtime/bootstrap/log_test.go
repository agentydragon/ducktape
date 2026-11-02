package bootstrap

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"runtime"
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
	events, err := replayed.Since(t.Context(), 0)
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

	suffix, err := replayed.Since(t.Context(), 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(suffix) != 2 || suffix[0].GetSequence() != 2 || suffix[1].GetSequence() != 3 {
		t.Fatalf("replayed suffix after cursor 1 = %v, want [2 3]", eventSequences(suffix))
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
	events, err := log.Since(t.Context(), 0)
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

func TestBootstrapLogReplayCursorDoesNotMissConcurrentAppends(t *testing.T) {
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
			runtime.Gosched()
		}
		writeErr <- nil
	}()

	cursor := uint64(0)
	var replayed []*sessionpb.InitializationEvent
	for {
		suffix, err := log.Since(t.Context(), cursor)
		if err != nil {
			t.Fatal(err)
		}
		for _, event := range suffix {
			if event.GetSequence() != cursor+1 {
				t.Fatalf("replay cursor jumped from %d to %d", cursor, event.GetSequence())
			}
			cursor = event.GetSequence()
			replayed = append(replayed, event)
		}
		select {
		case err := <-writeErr:
			if err != nil {
				t.Fatal(err)
			}
			final, err := log.Since(t.Context(), cursor)
			if err != nil {
				t.Fatal(err)
			}
			for _, event := range final {
				if event.GetSequence() != cursor+1 {
					t.Fatalf("final replay cursor jumped from %d to %d", cursor, event.GetSequence())
				}
				cursor = event.GetSequence()
				replayed = append(replayed, event)
			}
			if cursor != count || len(replayed) != count {
				t.Fatalf("replayed %d events through cursor %d, want %d", len(replayed), cursor, count)
			}
			for i, event := range replayed {
				if got := event.GetOutput().GetData(); len(got) != 1 || got[0] != byte(i) {
					t.Fatalf("event %d output = %v", event.GetSequence(), got)
				}
			}
			return
		default:
			runtime.Gosched()
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
	events, err := recovered.Since(t.Context(), 0)
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
