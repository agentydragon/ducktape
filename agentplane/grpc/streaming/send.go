// Package streaming provides bounded sends for long-lived gRPC server streams.
package streaming

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"time"
)

type abortWriteContextKey struct{}

// WithAbortWrite attaches an RPC-scoped transport abort callback. HTTP gRPC facades use it to
// interrupt a response write that remains blocked after the stream handler returns.
func WithAbortWrite(ctx context.Context, abort func() error) context.Context {
	return context.WithValue(ctx, abortWriteContextKey{}, abort)
}

func abortWrite(ctx context.Context) error {
	abort, _ := ctx.Value(abortWriteContextKey{}).(func() error)
	if abort == nil {
		return nil
	}
	return abort()
}

var (
	// ErrSendTimeout reports that a stream write exceeded the caller's bound.
	ErrSendTimeout = errors.New("gRPC stream write timed out")
	// ErrInvalidTimeout reports a non-positive caller-supplied write bound.
	ErrInvalidTimeout = errors.New("gRPC stream write timeout must be positive")
)

// SendWithin starts one write and returns when it finishes, ctx ends, or timeout elapses. If it returns before Send
// finishes, the caller must return from its RPC handler without another Send. Native gRPC releases the outstanding
// write when the handler returns; a facade can attach an abort callback with WithAbortWrite to release its HTTP write.
// Deriving a context alone cannot cancel a blocked gRPC Send.
func SendWithin[Frame any](ctx context.Context, timeout time.Duration, send func(*Frame) error, frame *Frame) error {
	if timeout <= 0 {
		return ErrInvalidTimeout
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	sent := make(chan error, 1)
	const (
		writeInProgress int32 = iota
		writeCompleted
		writeAborted
	)
	var writeState atomic.Int32
	go func() {
		err := send(frame)
		if writeState.CompareAndSwap(writeInProgress, writeCompleted) {
			sent <- err
		}
	}()

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case err := <-sent:
		return err
	case <-ctx.Done():
		if writeState.CompareAndSwap(writeInProgress, writeAborted) {
			return joinAbortError(ctx.Err(), abortWrite(ctx))
		}
		return <-sent
	case <-timer.C:
		if writeState.CompareAndSwap(writeInProgress, writeAborted) {
			return joinAbortError(ErrSendTimeout, abortWrite(ctx))
		}
		return <-sent
	}
}

func wrapAbortError(err error) error {
	if err == nil {
		return nil
	}
	return fmt.Errorf("abort blocked gRPC transport write: %w", err)
}

func joinAbortError(cause, abortErr error) error {
	if abortErr == nil {
		return cause
	}
	return errors.Join(cause, wrapAbortError(abortErr))
}
