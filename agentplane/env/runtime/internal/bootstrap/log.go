package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"sync"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
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
	rows, err := safe.ReaderListAll[*EventResource](ctx, store)
	if err != nil {
		return nil, err
	}
	// Distinct sequences in [1, count] prove density independent of list order.
	log := &Log{state: store, lastSequence: uint64(rows.Len())}
	seen := make([]bool, rows.Len())
	for row := range rows.All() {
		if err := validateEventResource(row); err != nil {
			return nil, err
		}
		event := row.TypedSpec().Value.Event
		sequence := event.GetSequence()
		if sequence > log.lastSequence {
			return nil, fmt.Errorf("bootstrap event sequence has a gap: sequence %d exceeds event count %d", sequence, log.lastSequence)
		}
		if seen[sequence-1] {
			return nil, fmt.Errorf("duplicate bootstrap event sequence %d", sequence)
		}
		seen[sequence-1] = true
		if isTerminalEvent(event) {
			if sequence != log.lastSequence {
				return nil, fmt.Errorf("bootstrap event sequence continues after terminal event %d", sequence)
			}
			log.terminal = proto.Clone(event).(*sessionpb.InitializationEvent)
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
