package streaming

import (
	"context"
	"time"
)

// ReceiveWithHeartbeat receives from a stream with a one-message buffer. It waits up to timeout
// for the first message and resets that interval after each successful consume. Calls to consume
// run synchronously. A receive error is returned unchanged unless receiveError maps it.
// ctx must control the stream and the caller must cancel it after return to release a blocked recv.
func ReceiveWithHeartbeat[Message any](
	ctx context.Context, timeout time.Duration, timeoutErr error,
	recv func() (*Message, error), receiveError func(error) error, consume func(*Message) error,
) error {
	type result struct {
		message *Message
		err     error
	}
	received := make(chan result, 1)
	go func() {
		for {
			message, err := recv()
			select {
			case received <- result{message, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	liveness := time.NewTimer(timeout)
	defer liveness.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-liveness.C:
			return timeoutErr
		case next := <-received:
			if next.err != nil {
				if receiveError != nil {
					return receiveError(next.err)
				}
				return next.err
			}
			if err := consume(next.message); err != nil {
				return err
			}
			if !liveness.Stop() {
				select {
				case <-liveness.C:
				default:
				}
			}
			liveness.Reset(timeout)
		}
	}
}
