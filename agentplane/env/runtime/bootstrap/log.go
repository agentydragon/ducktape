package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/protobuf/proto"
)

// Log is the durable, ordered event sequence for the Sandbox's one bootstrap attempt.
// The writer only creates resources, so appending output never rewrites its prefix.
type Log struct {
	state        state.State
	mu           sync.Mutex
	lastSequence uint64
	terminal     *sessionpb.InitializationEvent
	poisoned     error
}

func OpenLog(ctx context.Context, store state.State) (*Log, error) {
	events, err := readEvents(ctx, store)
	if err != nil {
		return nil, err
	}
	log := &Log{state: store}
	if len(events) > 0 {
		log.lastSequence = events[len(events)-1].GetSequence()
		if isTerminalEvent(events[len(events)-1]) {
			log.terminal = events[len(events)-1]
		}
	}
	return log, nil
}

func (l *Log) LastSequence() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastSequence
}

func (l *Log) Ended() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.terminal != nil
}

func (l *Log) terminalEvent(ctx context.Context) (*sessionpb.InitializationEvent, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.terminal == nil {
		return nil, nil
	}
	if l.poisoned != nil {
		return nil, fmt.Errorf("bootstrap event sequence is unavailable after uncertain write: %w", l.poisoned)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return proto.Clone(l.terminal).(*sessionpb.InitializationEvent), nil
}

func readEvents(ctx context.Context, reader controller.Reader) ([]*sessionpb.InitializationEvent, error) {
	rows, err := safe.ReaderListAll[*EventResource](ctx, reader)
	if err != nil {
		return nil, err
	}
	events := make([]*sessionpb.InitializationEvent, 0, rows.Len())
	var validationErr error
	rows.ForEach(func(value *EventResource) {
		if validationErr != nil {
			return
		}
		if err := validateEventResource(value); err != nil {
			validationErr = err
			return
		}
		events = append(events, proto.Clone(value.TypedSpec().Value.Event).(*sessionpb.InitializationEvent))
	})
	if validationErr != nil {
		return nil, validationErr
	}
	sort.Slice(events, func(i, j int) bool {
		return events[i].GetSequence() < events[j].GetSequence()
	})
	for i, event := range events {
		want := uint64(i) + 1
		if event.GetSequence() != want {
			return nil, fmt.Errorf("bootstrap event sequence has a gap: got %d, want %d", event.GetSequence(), want)
		}
		if i < len(events)-1 && isTerminalEvent(event) {
			return nil, fmt.Errorf("bootstrap event sequence continues after terminal event %d", event.GetSequence())
		}
	}
	return events, nil
}

func (l *Log) AppendOutput(ctx context.Context, attempt uint32, stream sessionpb.InitializationStream, data []byte) error {
	if len(data) == 0 {
		return errors.New("bootstrap output event is empty")
	}
	if stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT && stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDERR {
		return fmt.Errorf("unknown bootstrap output stream %d", stream)
	}
	return l.append(ctx, attempt, &sessionpb.InitializationEvent{
		Attempt: attempt,
		Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{
			Stream: stream, Data: append([]byte(nil), data...),
		}},
	})
}

func (l *Log) AppendResult(ctx context.Context, attempt uint32, exitCode int32) (*sessionpb.InitializeResult, error) {
	result := &sessionpb.InitializeResult{Executed: true, ExitCode: exitCode}
	event := &sessionpb.InitializationEvent{
		Attempt:     attempt,
		Observation: &sessionpb.InitializationEvent_Result{Result: proto.Clone(result).(*sessionpb.InitializeResult)},
	}
	if err := l.append(ctx, attempt, event); err != nil {
		return nil, err
	}
	return result, nil
}

func (l *Log) AppendInterrupted(ctx context.Context, attempt uint32) error {
	return l.append(ctx, attempt, &sessionpb.InitializationEvent{
		Attempt:     attempt,
		Observation: &sessionpb.InitializationEvent_Interrupted{Interrupted: &sessionpb.InitializationInterrupted{}},
	})
}

func (l *Log) append(ctx context.Context, attempt uint32, event *sessionpb.InitializationEvent) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.terminal != nil {
		return errors.New("bootstrap event sequence is terminal")
	}
	if l.poisoned != nil {
		return fmt.Errorf("bootstrap event sequence is unavailable after uncertain write: %w", l.poisoned)
	}
	if attempt != 1 {
		return fmt.Errorf("bootstrap attempt is %d, want 1", attempt)
	}
	if l.lastSequence == ^uint64(0) {
		return errors.New("bootstrap event sequence overflow")
	}
	event.Sequence = l.lastSequence + 1
	if err := validateEventObservation(event); err != nil {
		return err
	}
	target := EventResourceValue(event)
	if err := l.state.Create(ctx, target, state.WithCreateOwner(HostOwner)); err != nil {
		// COSI persists before publishing to its in-memory state. A failed Put can therefore
		// have committed durably while remaining invisible to ReaderGet. Never write another
		// event at this sequence until restart has reconstructed the durable log.
		l.poisoned = err
		return err
	}
	l.recordAppend(event)
	return nil
}

func (l *Log) recordAppend(event *sessionpb.InitializationEvent) {
	l.lastSequence = event.GetSequence()
	if isTerminalEvent(event) {
		l.terminal = proto.Clone(event).(*sessionpb.InitializationEvent)
	}
}

func validateEventObservation(event *sessionpb.InitializationEvent) error {
	if event == nil || event.GetAttempt() != 1 {
		return errors.New("bootstrap event must belong to attempt 1")
	}
	switch observation := event.GetObservation().(type) {
	case *sessionpb.InitializationEvent_Output:
		if observation.Output == nil || len(observation.Output.Data) == 0 {
			return errors.New("bootstrap output event is empty")
		}
		if observation.Output.Stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT && observation.Output.Stream != sessionpb.InitializationStream_INITIALIZATION_STREAM_STDERR {
			return fmt.Errorf("unknown bootstrap output stream %d", observation.Output.Stream)
		}
	case *sessionpb.InitializationEvent_Result:
		if observation.Result == nil || !observation.Result.Executed {
			return errors.New("bootstrap result event is incomplete")
		}
	case *sessionpb.InitializationEvent_Interrupted:
		if observation.Interrupted == nil {
			return errors.New("bootstrap interrupted event is incomplete")
		}
	default:
		return errors.New("bootstrap event has no observation")
	}
	return nil
}

func isTerminalEvent(event *sessionpb.InitializationEvent) bool {
	return event.GetResult() != nil || event.GetInterrupted() != nil
}
