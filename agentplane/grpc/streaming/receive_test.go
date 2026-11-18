package streaming

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"
	"time"
)

func TestReceiveWithHeartbeatResetsAfterConsumerReturns(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		const timeout = time.Second
		timeoutErr := errors.New("no heartbeat")
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		messages := make(chan *int)
		finishConsume := make(chan struct{})
		result := make(chan error, 1)
		go func() {
			result <- ReceiveWithHeartbeat(ctx, timeout, timeoutErr, func() (*int, error) {
				select {
				case message := <-messages:
					return message, nil
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			}, nil, func(*int) error {
				select {
				case <-finishConsume:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			})
		}()
		messages <- new(int)
		synctest.Wait()

		// An accepted message gets a fresh interval even when processing outlasts the timer.
		time.Sleep(2 * timeout)
		close(finishConsume)
		synctest.Wait()
		time.Sleep(timeout - time.Nanosecond)
		select {
		case err := <-result:
			t.Fatalf("receive returned during the new heartbeat interval: %v", err)
		default:
		}

		time.Sleep(time.Nanosecond)
		synctest.Wait()
		select {
		case err := <-result:
			if err != timeoutErr {
				t.Fatalf("receive returned %v, want %v", err, timeoutErr)
			}
		default:
			t.Fatal("receive did not stop after the heartbeat interval")
		}
	})
}
