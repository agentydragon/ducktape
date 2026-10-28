package ingress

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/stream"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	heartbeatInterval = 10 * time.Second
	streamLiveness    = 30 * time.Second
	inFlightLimit     = 1024
	pageSize          = 128
)

// Source is the durable history a runner can publish for one Session.
type Source struct {
	ID       string
	Announce func() *runnerpb.Attached
	Journal  *journal.Journal
}

// Client publishes journal prefixes to the History Service.
type Client struct {
	rpc   ingresspb.RunnerIngressClient
	store *retained.Store
	onAck func(string, retained.Acknowledgement) error
}

// NewClient binds the RPC client to durable acknowledgement storage and its
// observation owner. Deliver receives the outgoing-authenticated context.
func NewClient(rpc ingresspb.RunnerIngressClient, store *retained.Store, onAck func(string, retained.Acknowledgement) error) *Client {
	return &Client{rpc: rpc, store: store, onAck: onAck}
}

// Deliver retries one Session's push stream until it is sealed or canceled.
func (c *Client) Deliver(ctx context.Context, source Source) error {
	if source.ID == "" || source.Announce == nil || source.Journal == nil {
		return errors.New("ingress source requires an ID, announce function, and journal")
	}
	return stream.Retry(ctx, "ingress "+source.ID, func(ctx context.Context) (bool, error) {
		return c.pushOnce(ctx, source)
	}, isLocalFailure)
}

type localError struct{ err error }

func (e *localError) Error() string { return e.err.Error() }
func (e *localError) Unwrap() error { return e.err }

func isLocalFailure(err error) bool {
	var local *localError
	var storage *journal.StorageError
	return errors.As(err, &local) || errors.As(err, &storage)
}

func (c *Client) acknowledge(source Source, ack retained.Acknowledgement) error {
	if err := c.store.WriteAcknowledgement(source.ID, ack); err != nil {
		return &localError{err}
	}
	if c.onAck != nil {
		if err := c.onAck(source.ID, ack); err != nil {
			return &localError{err}
		}
	}
	return nil
}

func (c *Client) pushOnce(ctx context.Context, source Source) (bool, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.rpc.Push(ctx)
	if err != nil {
		return false, err
	}
	if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Announce{Announce: source.Announce()}}); err != nil {
		return false, err
	}
	type receivedAck struct {
		ack *ingresspb.Acknowledgement
		err error
	}
	received := make(chan receivedAck, 1)
	var receiver sync.WaitGroup
	receiver.Add(1)
	go func() {
		defer receiver.Done()
		for {
			ack, err := stream.Recv()
			select {
			case received <- receivedAck{ack, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	defer func() { cancel(); receiver.Wait() }()

	timer := time.NewTimer(streamLiveness)
	defer timer.Stop()
	var first receivedAck
	select {
	case first = <-received:
	case <-timer.C:
		return false, errors.New("no first acknowledgement from ingress")
	case <-ctx.Done():
		return false, ctx.Err()
	}
	if first.err != nil {
		return false, first.err
	}
	if first.ack == nil {
		return false, status.Error(codes.InvalidArgument, "empty ingress acknowledgement")
	}
	if first.ack.Cursor > source.Journal.LastCursor() {
		return false, status.Error(codes.FailedPrecondition, "ingress cursor exceeds the journal")
	}
	acknowledged, sent := first.ack.Cursor, first.ack.Cursor
	if err := c.acknowledge(source, retained.Acknowledgement{Cursor: acknowledged, Sealed: first.ack.Sealed}); err != nil {
		return false, err
	}
	if first.ack.Sealed {
		return true, nil
	}

	progressed, sealedSent := false, false
	lastReceived, lastSent := time.Now(), time.Now()
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()
	var waiter *journalWaiter
	stopWaiter := func() error {
		if waiter == nil {
			return nil
		}
		err := waiter.stop()
		waiter = nil
		return err
	}
	defer func() { _ = stopWaiter() }()
	for {
		if !sealedSent && sent < acknowledged+inFlightLimit {
			limit := pageSize
			if remaining := acknowledged + inFlightLimit - sent; remaining < uint64(limit) {
				limit = int(remaining)
			}
			entries, err := source.Journal.Since(ctx, sent, limit)
			if err != nil {
				return progressed, &localError{err}
			}
			for _, entry := range entries {
				sent = entry.Cursor
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Entry{Entry: entry}}); err != nil {
					return progressed, err
				}
				lastSent = time.Now()
			}
			if len(entries) > 0 {
				continue
			}
			if seal := source.Journal.SealState(); seal != nil && sent == seal.Cursor {
				uid := seal.IncarnationUID
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Sealed{Sealed: &runnerpb.Sealed{Cursor: seal.Cursor, IncarnationUid: &uid}}}); err != nil {
					return progressed, err
				}
				sealedSent = true
				if err := stream.CloseSend(); err != nil {
					return progressed, err
				}
			} else if waiter == nil {
				waiter = newJournalWaiter(ctx, source.Journal, sent)
			}
		}

		var journalWake <-chan error
		if waiter != nil {
			journalWake = waiter.done
		}
		select {
		case <-ctx.Done():
			return progressed, ctx.Err()
		case message := <-received:
			if err := stopWaiter(); err != nil {
				return progressed, &localError{err}
			}
			if message.err != nil {
				if message.err == io.EOF {
					message.err = errors.New("ingress ended before acknowledging the seal")
				}
				return progressed, message.err
			}
			if message.ack == nil {
				return progressed, status.Error(codes.InvalidArgument, "empty ingress acknowledgement")
			}
			ack := message.ack
			lastReceived = time.Now()
			if ack.Cursor < acknowledged || ack.Cursor > sent {
				return progressed, status.Error(codes.FailedPrecondition, fmt.Sprintf("invalid ingress acknowledgement %d after %d, sent %d", ack.Cursor, acknowledged, sent))
			}
			progressed = progressed || ack.Cursor > acknowledged
			acknowledged = ack.Cursor
			if ack.Sealed {
				seal := source.Journal.SealState()
				if !sealedSent || seal == nil || ack.Cursor != seal.Cursor {
					return progressed, status.Error(codes.FailedPrecondition, "ingress acknowledged an unsent seal")
				}
			}
			if err := c.acknowledge(source, retained.Acknowledgement{Cursor: ack.Cursor, Sealed: ack.Sealed}); err != nil {
				return progressed, err
			}
			if ack.Sealed {
				return true, nil
			}
		case err := <-journalWake:
			_ = stopWaiter()
			if err != nil {
				if ctx.Err() != nil {
					return progressed, ctx.Err()
				}
				return progressed, &localError{err}
			}
		case <-ticker.C:
			if time.Since(lastReceived) > streamLiveness {
				return progressed, errors.New("no frame from ingress")
			}
			if !sealedSent && time.Since(lastSent) >= heartbeatInterval {
				if err := stream.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Heartbeat{Heartbeat: &ingresspb.Heartbeat{}}}); err != nil {
					return progressed, err
				}
				lastSent = time.Now()
			}
		}
	}
}

type journalWaiter struct {
	cancel context.CancelFunc
	done   chan error
	wait   sync.WaitGroup
}

func newJournalWaiter(ctx context.Context, source *journal.Journal, cursor uint64) *journalWaiter {
	waitCtx, cancel := context.WithCancel(ctx)
	w := &journalWaiter{cancel: cancel, done: make(chan error, 1)}
	w.wait.Add(1)
	go func() {
		defer w.wait.Done()
		w.done <- source.WaitBeyond(waitCtx, cursor)
	}()
	return w
}

func (w *journalWaiter) stop() error {
	w.cancel()
	w.wait.Wait()
	select {
	case err := <-w.done:
		if !errors.Is(err, context.Canceled) {
			return err
		}
	default:
	}
	return nil
}
