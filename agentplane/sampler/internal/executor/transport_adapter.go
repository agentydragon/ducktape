package executor

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor/transport"
)

func operationValue[T any](operation transport.Operation[T], err error) (*T, error) {
	if err != nil {
		return nil, err
	}
	if operation.Outcome != transport.Completed {
		if operation.Failure != nil {
			return nil, operation.Failure
		}
		return nil, errors.New("executor returned an incomplete operation without failure facts")
	}
	return &operation.Value, nil
}

func (e *Executor) close() error {
	if e.transport == nil {
		return nil
	}
	return e.transport.Close()
}

func (e *Executor) ProcessChanges() <-chan struct{} {
	return e.processChanges.Changes()
}

func (e *Executor) signalProcessChange() {
	e.processChanges.SignalProcessChange()
}

func (e *Executor) retainedExecutorSession(ctx context.Context) string {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil || conversation.ExecutorSession == nil {
		return ""
	}
	return *conversation.ExecutorSession
}
