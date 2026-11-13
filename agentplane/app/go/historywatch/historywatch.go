// Package historywatch follows History Service summary watches for app-local work.
package historywatch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
)

// ErrInvalidSessionID means the History Service returned a Session ID that the app cannot use as a
// canonical thread key.
var ErrInvalidSessionID = errors.New("invalid History Session ID")

// Stream is the receive side of a generated WatchSessionSummaries stream.
type Stream interface {
	Recv() (*historypb.WatchSessionSummariesResponse, error)
}

// Client is the narrow surface needed from a dedicated History Service client. Its adapter should
// call the generated HistoryServiceClient.WatchSessionSummaries method and Redial should close only
// this watch worker's channel so unrelated History reads remain available.
type Client interface {
	WatchSessionSummaries(context.Context, *historypb.WatchSessionSummariesRequest) (Stream, error)
	Redial()
}

// ThreadOpener is implemented by threads.Store.
type ThreadOpener interface {
	OpenThread(context.Context, uuid.UUID) error
}

// Options controls recovery from a failed watch stream.
type Options struct {
	RetryInterval     time.Duration
	FreshChannelAfter int
	Logger            *slog.Logger
}

func (o Options) normalized() (Options, error) {
	if o.RetryInterval <= 0 {
		return Options{}, errors.New("history watch retry interval must be positive")
	}
	if o.FreshChannelAfter == 0 {
		o.FreshChannelAfter = 4
	}
	if o.FreshChannelAfter < 1 {
		return Options{}, errors.New("history watch fresh-channel threshold must be positive")
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	return o, nil
}

type rewatch struct {
	client            Client
	retryInterval     time.Duration
	freshChannelAfter int
	logger            *slog.Logger
	follow            func(context.Context, func()) error
}

func (w rewatch) run(ctx context.Context) error {
	consecutiveFailures := 0
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := w.follow(ctx, func() { consecutiveFailures = 0 })
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err == nil { // reconnect_required is a planned renewal, not a failure.
			consecutiveFailures = 0
			continue
		}
		var permanent *permanentError
		if errors.As(err, &permanent) {
			return err
		}
		consecutiveFailures++
		w.logger.WarnContext(ctx, "History Service watch failed; reconnecting", "error", err, "consecutive_failures", consecutiveFailures)
		if consecutiveFailures%w.freshChannelAfter == 0 {
			w.logger.WarnContext(ctx, "history watch failed repeatedly; dialing a fresh channel", "consecutive_failures", consecutiveFailures)
			w.client.Redial()
		}
		timer := time.NewTimer(w.retryInterval)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			return ctx.Err()
		case <-timer.C:
		}
	}
}

type permanentError struct{ err error }

func (e *permanentError) Error() string { return e.err.Error() }
func (e *permanentError) Unwrap() error { return e.err }

func permanent(err error) error { return &permanentError{err: err} }

func receive(stream Stream) (*historypb.WatchSessionSummariesResponse, error) {
	response, err := stream.Recv()
	if errors.Is(err, io.EOF) {
		return nil, errors.New("History Service watch ended without reconnect_required")
	}
	if err != nil {
		return nil, err
	}
	if response == nil {
		return nil, errors.New("History Service watch returned an empty response")
	}
	return response, nil
}

// HistoryWatch wakes the caller when the stream's session notice or a later fold change arrives.
type HistoryWatch struct {
	client  Client
	changed func()
	rewatch rewatch
}

// NewHistoryWatch creates a summary watcher. A zero FreshChannelAfter selects the Python-compatible
// default of four consecutive failed streams.
func NewHistoryWatch(client Client, changed func(), options Options) (*HistoryWatch, error) {
	if client == nil || changed == nil {
		return nil, errors.New("history watch client and changed callback are required")
	}
	options, err := options.normalized()
	if err != nil {
		return nil, err
	}
	w := &HistoryWatch{client: client, changed: changed}
	w.rewatch = rewatch{
		client: client, retryInterval: options.RetryInterval,
		freshChannelAfter: options.FreshChannelAfter, logger: options.Logger,
		follow: w.follow,
	}
	return w, nil
}

// Run follows the watch until ctx is cancelled or a non-retryable local protocol error occurs.
func (w *HistoryWatch) Run(ctx context.Context) error { return w.rewatch.run(ctx) }

func (w *HistoryWatch) follow(ctx context.Context, progress func()) error {
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := w.client.WatchSessionSummaries(streamCtx, &historypb.WatchSessionSummariesRequest{})
	if err != nil {
		return err
	}
	for {
		response, err := receive(stream)
		if err != nil {
			return err
		}
		switch {
		case response.GetReconnectRequired() != nil:
			return nil
		case response.GetChanged() != nil:
			w.changed()
			progress()
		case response.GetSessionRecorded() != nil:
			return fmt.Errorf("unexpected session_recorded observation on summary-only watch")
		default:
			return errors.New("History Service watch response has no observation")
		}
	}
}

// Sessions materializes every History Session in the app's thread table. The position stays
// behind any failed open so the next stream offers that Session again.
type Sessions struct {
	client        Client
	threads       ThreadOpener
	rewatch       rewatch
	openedThrough uint64
}

// NewSessions creates a worker resuming from position zero.
func NewSessions(client Client, threads ThreadOpener, options Options) (*Sessions, error) {
	if client == nil || threads == nil {
		return nil, errors.New("history watch client and thread store are required")
	}
	options, err := options.normalized()
	if err != nil {
		return nil, err
	}
	w := &Sessions{client: client, threads: threads}
	w.rewatch = rewatch{
		client: client, retryInterval: options.RetryInterval,
		freshChannelAfter: options.FreshChannelAfter, logger: options.Logger,
		follow: w.follow,
	}
	return w, nil
}

// Run processes sessions until ctx is cancelled or an invalid Session ID is received.
func (w *Sessions) Run(ctx context.Context) error { return w.rewatch.run(ctx) }

func (w *Sessions) follow(ctx context.Context, progress func()) error {
	after := w.openedThrough
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := w.client.WatchSessionSummaries(streamCtx, &historypb.WatchSessionSummariesRequest{RecordedAfter: &after})
	if err != nil {
		return err
	}
	spanParent := ctx
	if tracedStream, ok := stream.(interface{ Context() context.Context }); ok {
		spanParent = tracedStream.Context()
	}
	for {
		response, err := receive(stream)
		if err != nil {
			return err
		}
		switch {
		case response.GetReconnectRequired() != nil:
			return nil
		case response.GetChanged() != nil:
			// The changed notice asks summary readers to refresh; it names no Session.
			continue
		case response.GetSessionRecorded() != nil:
			recorded := response.GetSessionRecorded()
			id, err := uuid.Parse(recorded.GetSessionId())
			if err != nil || id.String() != recorded.GetSessionId() {
				return permanent(fmt.Errorf("%w: %q", ErrInvalidSessionID, recorded.GetSessionId()))
			}
			recordingCtx, span := observability.Start(spanParent, "agentplane.session.recording.record")
			span.SetAttributes(attribute.Int64("agentplane.session.recording.position", int64(recorded.GetPosition())))
			if err := w.threads.OpenThread(recordingCtx, id); err != nil {
				observability.End(span, err)
				return fmt.Errorf("open app thread for History Session %s: %w", id, err)
			}
			observability.End(span, nil)
			w.openedThrough = recorded.GetPosition()
			progress()
		default:
			return errors.New("History Service watch response has no observation")
		}
	}
}
