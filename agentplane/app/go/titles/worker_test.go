package titles

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type countingProcessor struct {
	calls atomic.Int64
	err   error
}

func (p *countingProcessor) ProcessPending(context.Context) error {
	p.calls.Add(1)
	return p.err
}

func runWorkerUntilCancelled(t *testing.T, worker *ThreadTitleWorker) context.CancelFunc {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- worker.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Errorf("Run returned %v, want context.Canceled", err)
			}
		case <-time.After(3 * time.Second):
			t.Error("title worker did not stop after cancellation")
		}
	})
	return cancel
}

func TestThreadTitleWorkerProcessesChangesAndRenewsQuietReads(t *testing.T) {
	history := &fakeHistory{watchScripts: []fakeWatchScript{{responses: []*historypb.WatchSessionSummariesResponse{
		changedWatchResponse(), reconnectWatchResponse(),
	}}, {responses: []*historypb.WatchSessionSummariesResponse{changedWatchResponse()}}}}
	processor := &countingProcessor{}
	worker, err := NewThreadTitleWorker(history, processor, WorkerOptions{
		RetryInterval: time.Millisecond,
		ReadTimeout:   20 * time.Millisecond,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	cancel := runWorkerUntilCancelled(t, worker)
	waitFor(t, func() bool {
		requests, _ := history.watchSnapshot()
		return requests >= 3 && processor.calls.Load() >= 2
	})
	_, redials := history.watchSnapshot()
	if redials != 0 {
		t.Fatalf("planned reconnect/read timeout caused %d redials", redials)
	}
	cancel()
}

func TestThreadTitleWorkerRetriesAndRedialsAfterFourFailedStreams(t *testing.T) {
	history := &fakeHistory{watchScripts: []fakeWatchScript{
		{watchErr: status.Error(codes.Unavailable, "dial failed")},
		{recvErr: status.Error(codes.DeadlineExceeded, "deadline")},
		{recvErr: io.EOF},
		{recvErr: status.Error(codes.Unavailable, "stream failed")},
		{responses: []*historypb.WatchSessionSummariesResponse{changedWatchResponse(), reconnectWatchResponse()}},
	}}
	processor := &countingProcessor{}
	worker, err := NewThreadTitleWorker(history, processor, WorkerOptions{
		RetryInterval: time.Millisecond,
		ReadTimeout:   time.Second,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	cancel := runWorkerUntilCancelled(t, worker)
	waitFor(t, func() bool {
		requests, redials := history.watchSnapshot()
		return requests >= 6 && redials == 1 && processor.calls.Load() == 1
	})
	cancel()
}

func TestThreadTitleWorkerDefaultsToOneSecondRetryAndSixtySecondRenewal(t *testing.T) {
	worker, err := NewThreadTitleWorker(&fakeHistory{}, &countingProcessor{}, WorkerOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if worker.options.RetryInterval != time.Second || worker.options.ReadTimeout != 60*time.Second ||
		worker.options.FreshChannelAfter != 4 {
		t.Fatalf("worker defaults = %+v", worker.options)
	}
}
