package titles

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/historywatch"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
)

const (
	defaultWatchRetryInterval = time.Second
	defaultWatchReadTimeout   = 60 * time.Second
	defaultFreshChannelAfter  = 4
)

// WorkerOptions controls the summary watch's retry, per-read renewal, and channel-redial cadence.
type WorkerOptions struct {
	RetryInterval     time.Duration
	ReadTimeout       time.Duration
	FreshChannelAfter int
	Logger            *slog.Logger
}

func (o WorkerOptions) normalized() (WorkerOptions, error) {
	if o.RetryInterval == 0 {
		o.RetryInterval = defaultWatchRetryInterval
	}
	if o.ReadTimeout == 0 {
		o.ReadTimeout = defaultWatchReadTimeout
	}
	if o.FreshChannelAfter == 0 {
		o.FreshChannelAfter = defaultFreshChannelAfter
	}
	if o.RetryInterval < 0 || o.ReadTimeout < 0 || o.FreshChannelAfter < 1 {
		return WorkerOptions{}, errors.New("title worker intervals and fresh-channel threshold must be positive")
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	return o, nil
}

// PendingTitleProcessor processes the pending title claims after a History change.
type PendingTitleProcessor interface {
	ProcessPending(context.Context) error
}

// ThreadTitleWorker wakes first-message title processing from committed History changes and renews
// a quiet watch after one read interval.
type ThreadTitleWorker struct {
	history History
	titles  PendingTitleProcessor
	options WorkerOptions
}

// NewThreadTitleWorker creates the background title worker. A zero ReadTimeout uses the Python
// worker's 60-second read/renew interval; a zero retry interval uses one second.
func NewThreadTitleWorker(history History, titles PendingTitleProcessor, options WorkerOptions) (*ThreadTitleWorker, error) {
	if history == nil || titles == nil {
		return nil, errors.New("title worker History client and processor are required")
	}
	options, err := options.normalized()
	if err != nil {
		return nil, err
	}
	return &ThreadTitleWorker{history: history, titles: titles, options: options}, nil
}

// Run watches until ctx is cancelled.
func (w *ThreadTitleWorker) Run(ctx context.Context) error {
	consecutiveFailures := 0
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := w.follow(ctx, func() { consecutiveFailures = 0 })
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err == nil { // planned renewal or a quiet read timeout.
			consecutiveFailures = 0
			continue
		}
		consecutiveFailures++
		w.options.Logger.WarnContext(ctx, "History Service title watch failed; reconnecting", "error", err, "consecutive_failures", consecutiveFailures)
		if consecutiveFailures%w.options.FreshChannelAfter == 0 {
			w.options.Logger.WarnContext(ctx, "title watch failed repeatedly; dialing a fresh channel", "consecutive_failures", consecutiveFailures)
			w.history.Redial()
		}
		timer := time.NewTimer(w.options.RetryInterval)
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

func (w *ThreadTitleWorker) follow(ctx context.Context, progress func()) error {
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := w.history.WatchSessionSummaries(streamCtx, &historypb.WatchSessionSummariesRequest{})
	if err != nil {
		return err
	}
	for {
		response, renewed, err := receiveBeforeRenew(streamCtx, stream, w.options.ReadTimeout)
		if err != nil {
			return err
		}
		if renewed {
			return nil
		}
		switch {
		case response.GetReconnectRequired() != nil:
			return nil
		case response.GetChanged() != nil:
			if err := w.titles.ProcessPending(ctx); err != nil {
				return err
			}
			progress()
		case response.GetSessionOpened() != nil:
			return errors.New("unexpected session_opened observation on title summary watch")
		default:
			return errors.New("History Service title watch response has no observation")
		}
	}
}

type receiveResult struct {
	response *historypb.WatchSessionSummariesResponse
	err      error
}

// receiveBeforeRenew gives each Recv its own timeout, matching the Python worker's wait_for(anext,
// 60s). On timeout, follow returns and cancels the stream before opening its planned replacement.
func receiveBeforeRenew(
	ctx context.Context,
	stream historywatch.Stream,
	timeout time.Duration,
) (*historypb.WatchSessionSummariesResponse, bool, error) {
	result := make(chan receiveResult, 1)
	go func() {
		response, err := stream.Recv()
		result <- receiveResult{response: response, err: err}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return nil, false, ctx.Err()
	case <-timer.C:
		return nil, true, nil
	case received := <-result:
		if errors.Is(received.err, io.EOF) {
			return nil, false, errors.New("History Service title watch ended without reconnect_required")
		}
		if received.err != nil {
			return nil, false, received.err
		}
		if received.response == nil {
			return nil, false, errors.New("History Service title watch returned an empty response")
		}
		return received.response, false, nil
	}
}
