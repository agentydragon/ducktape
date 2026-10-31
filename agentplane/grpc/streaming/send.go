// Package streaming provides bounded sends for long-lived gRPC server streams.
package streaming

import (
	"context"
	"errors"
	"sync/atomic"
	"time"
)

var (
	// ErrSendTimeout reports that a stream write exceeded the caller's bound.
	ErrSendTimeout = errors.New("gRPC stream write timed out")
	// ErrInvalidTimeout reports a non-positive caller-supplied write bound.
	ErrInvalidTimeout = errors.New("gRPC stream write timeout must be positive")
)

// SendWithin starts one write and returns when it finishes, ctx ends, or timeout elapses. If it returns before Send
// finishes, the caller must return from its RPC handler without another Send: gRPC releases the outstanding write when
// the handler returns. Deriving a context alone cannot cancel a blocked gRPC Send.
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
		writeAbandoned
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
		if writeState.CompareAndSwap(writeInProgress, writeAbandoned) {
			return ctx.Err()
		}
		return <-sent
	case <-timer.C:
		if writeState.CompareAndSwap(writeInProgress, writeAbandoned) {
			return ErrSendTimeout
		}
		return <-sent
	}
}
