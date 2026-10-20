package api

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// watcher is what a watch stream needs besides the state it follows.
type watcher struct {
	wakeups *wakeups.Hub
	// timeout bounds each read and each write to the stream.
	timeout time.Duration
	// lease ends a stream by itself.
	lease time.Duration
	// draining ends every stream when the server shuts down.
	draining context.Context
}

// reading is one read of the state a watch follows.
type reading[Frame any] struct {
	// frame is the snapshot, ready to send.
	frame *Frame
	// shown identifies what the snapshot displays: two readings with the same shown are one state, whenever they
	// were read.
	shown string
	// changesAt is when the state next changes without a commit; the zero time is never.
	changesAt time.Time
}

// shownKey identifies what a snapshot displays. The snapshot must not carry the time it was read at, and extra
// is what is displayed without being in the snapshot.
func shownKey(snapshot proto.Message, extra []bool) (string, error) {
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(snapshot)
	if err != nil {
		return "", err
	}
	for _, flag := range extra {
		if flag {
			encoded = append(encoded, 1)
		} else {
			encoded = append(encoded, 0)
		}
	}
	return string(encoded), nil
}

// follow sends the snapshot read returns, then another whenever what it shows differs from the last one sent: after a
// wakeup, and at the time the state changes without one. It ends with reconnect once the lease ends or the server
// drains. A wakeup only prompts a read: what a watch shows is in the tables, so a notification lost on a replica
// or a dropped LISTEN connection delays a snapshot to the next deadline at most and never shows a wrong state.
func follow[Frame any](
	ctx context.Context, w watcher, stream grpc.ServerStreamingServer[Frame],
	read func(context.Context) (reading[Frame], error), reconnect *Frame,
) error {
	leased, endLease := context.WithTimeout(ctx, w.lease)
	defer endLease()
	defer context.AfterFunc(w.draining, endLease)()
	woken, unsubscribe, err := w.wakeups.Subscribe(store.WorkChannel)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	var shown *string
	for {
		// Cleared before the read, so a commit after it leaves a wakeup for the next one.
		select {
		case <-woken:
		default:
		}
		seen, err := readWithin(ctx, w.timeout, read)
		if err != nil {
			return err
		}
		if shown == nil || *shown != seen.shown {
			if err := sendWithin(stream, w.timeout, seen.frame); err != nil {
				return err
			}
			shown = &seen.shown
		}
		if awaitChange(leased, woken, seen.changesAt) {
			if ctx.Err() != nil {
				return status.FromContextError(ctx.Err()).Err()
			}
			// The lease ended or the server is draining; the caller connects again with fresh authorization.
			return sendWithin(stream, w.timeout, reconnect)
		}
	}
}

func readWithin[Frame any](ctx context.Context, timeout time.Duration, read func(context.Context) (reading[Frame], error)) (reading[Frame], error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	seen, err := read(ctx)
	if err != nil {
		return seen, failure(ctx, err)
	}
	return seen, nil
}

// awaitChange blocks until a wakeup, the time the state changes at, or the end of the lease, and reports whether
// the lease ended.
func awaitChange(leased context.Context, woken <-chan struct{}, changesAt time.Time) bool {
	var due <-chan time.Time
	if !changesAt.IsZero() {
		timer := time.NewTimer(time.Until(changesAt))
		defer timer.Stop()
		due = timer.C
	}
	select {
	case <-woken:
		return false
	case <-due:
		return false
	case <-leased.Done():
		return true
	}
}

// sendWithin writes within the timeout: a caller that stops reading would otherwise hold the stream open past its
// lease.
func sendWithin[Frame any](stream grpc.ServerStreamingServer[Frame], timeout time.Duration, frame *Frame) error {
	sent := make(chan error, 1)
	// A write that outlives the timeout returns when the handler does, which ends the stream.
	go func() { sent <- stream.Send(frame) }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case err := <-sent:
		return err
	case <-timer.C:
		return status.Error(codes.DeadlineExceeded, "notification watch write deadline expired")
	}
}
