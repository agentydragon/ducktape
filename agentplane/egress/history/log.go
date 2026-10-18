package history

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/sethvargo/go-retry"
)

const (
	// idleCleanup is how long the writer waits for a record before cleaning up anyway.
	idleCleanup   = time.Minute
	writeAttempts = 3
	retryBase     = time.Second
)

var (
	errShuttingDown = errors.New("shutting down")
	errQueueFull    = errors.New("queue full")
)

// batchStore is the Store as the writer uses it.
type batchStore interface {
	Append(ctx context.Context, records []decision.Record) error
	Cleanup(ctx context.Context) error
}

// LogConfig bounds a Log.
type LogConfig struct {
	// QueueSize is how many records may wait for the writer before new ones are dropped.
	QueueSize int
	// BatchSize is the most records one write carries.
	BatchSize int
	// FlushTimeout is how long Close waits for queued records to be written.
	FlushTimeout time.Duration
}

// Health is the Log's counters, served as `decisionHistory` by the admin port.
type Health struct {
	Accepted           int `json:"accepted"`
	Acknowledged       int `json:"acknowledged"`
	DroppedOverflow    int `json:"dropped_overflow"`
	DroppedUnavailable int `json:"dropped_unavailable"`
	DroppedExpired     int `json:"dropped_expired"`
	DroppedShutdown    int `json:"dropped_shutdown"`
	WriteFailures      int `json:"write_failures"`
	CleanupFailures    int `json:"cleanup_failures"`
	// Available is whether the latest write succeeded; nil before the first.
	Available     *bool `json:"available"`
	Queued        int   `json:"queued"`
	WriterRunning bool  `json:"writer_running"`
}

// Log is bounded, best-effort persistence: admission records a decision without waiting for the
// database, and a decision that cannot be written is counted and dropped.
type Log struct {
	store        batchStore
	retention    time.Duration
	batchSize    int
	flushTimeout time.Duration
	backoff      func() retry.Backoff
	queue        chan decision.Record
	stop         context.CancelFunc
	done         chan struct{}

	mu      sync.Mutex
	closing bool
	// pending counts records accepted and not yet written or dropped; idle is closed while it is zero.
	pending int
	idle    chan struct{}
	health  Health
}

// NewLog starts the writer; Close stops it. The caller closes the store's database after Close.
func NewLog(store *Store, config LogConfig) (*Log, error) {
	return newLog(store, store.retention, config, func() retry.Backoff {
		return retry.WithMaxRetries(writeAttempts-1, retry.NewExponential(retryBase))
	})
}

func newLog(store batchStore, retention time.Duration, config LogConfig, backoff func() retry.Backoff) (*Log, error) {
	if config.QueueSize < 1 || config.BatchSize < 1 {
		return nil, fmt.Errorf("queue and batch sizes must be positive: %+v", config)
	}
	ctx, stop := context.WithCancel(context.Background())
	l := &Log{
		store: store, retention: retention, batchSize: config.BatchSize, flushTimeout: config.FlushTimeout,
		backoff: backoff, queue: make(chan decision.Record, config.QueueSize), stop: stop,
		done: make(chan struct{}), idle: make(chan struct{}),
	}
	close(l.idle)
	go l.run(ctx)
	return l, nil
}

// Record queues a decision for writing and never blocks.
func (l *Log) Record(record decision.Record) {
	if err := l.enqueue(record); err != nil {
		slog.Warn("decision history dropped", "reason", err.Error())
		return
	}
	slog.Info("egress decision", "decision", record)
}

func (l *Log) enqueue(record decision.Record) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closing {
		l.health.DroppedShutdown++
		return errShuttingDown
	}
	select {
	case l.queue <- record:
	default:
		l.health.DroppedOverflow++
		return errQueueFull
	}
	l.health.Accepted++
	if l.pending == 0 {
		l.idle = make(chan struct{})
	}
	l.pending++
	return nil
}

// Health is a snapshot of the counters.
func (l *Log) Health() Health {
	l.mu.Lock()
	defer l.mu.Unlock()
	health := l.health
	health.Queued = len(l.queue)
	select {
	case <-l.done:
	default:
		health.WriterRunning = true
	}
	return health
}

// Flush waits until every accepted record has been written or dropped.
func (l *Log) Flush(ctx context.Context) error {
	l.mu.Lock()
	idle := l.idle
	l.mu.Unlock()
	select {
	case <-idle:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close stops accepting records, gives the writer the flush timeout to drain the queue, and counts
// whatever is left as dropped. It does not wait on a database operation the timeout cut short.
func (l *Log) Close() {
	l.mu.Lock()
	l.closing = true
	l.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), l.flushTimeout)
	defer cancel()
	if l.Flush(ctx) != nil {
		slog.Warn("decision history shutdown deadline reached")
	}
	l.stop()
	<-l.done
	for {
		select {
		case <-l.queue:
			l.settle(1)
			l.tally(func(h *Health) { h.DroppedShutdown++ })
		default:
			return
		}
	}
}

func (l *Log) tally(update func(*Health)) {
	l.mu.Lock()
	defer l.mu.Unlock()
	update(&l.health)
}

func (h *Health) setAvailable(available bool) { h.Available = &available }

func (l *Log) settle(n int) {
	if n == 0 {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.pending -= n; l.pending == 0 {
		close(l.idle)
	}
}

func (l *Log) run(ctx context.Context) {
	defer close(l.done)
	for ctx.Err() == nil {
		batch := l.collect(ctx)
		l.write(ctx, batch)
		l.settle(len(batch))
		l.cleanup(ctx)
	}
}

// collect waits for a record, up to the idle interval, then takes as many more as a batch holds.
func (l *Log) collect(ctx context.Context) []decision.Record {
	idle := time.NewTimer(idleCleanup)
	defer idle.Stop()
	var batch []decision.Record
	select {
	case record := <-l.queue:
		batch = append(batch, record)
	case <-idle.C:
		return nil
	case <-ctx.Done():
		return nil
	}
	for len(batch) < l.batchSize {
		select {
		case record := <-l.queue:
			batch = append(batch, record)
		default:
			return batch
		}
	}
	return batch
}

func (l *Log) write(ctx context.Context, batch []decision.Record) {
	cutoff := time.Now().Add(-l.retention)
	current := make([]decision.Record, 0, len(batch))
	for _, record := range batch {
		if !record.At.Before(cutoff) {
			current = append(current, record)
		}
	}
	if expired := len(batch) - len(current); expired > 0 {
		l.tally(func(h *Health) { h.DroppedExpired += expired })
	}
	if len(current) == 0 {
		return
	}
	err := retry.Do(ctx, l.backoff(), func(ctx context.Context) error {
		err := l.store.Append(ctx, current)
		if err == nil || ctx.Err() != nil {
			return err
		}
		l.tally(func(h *Health) {
			h.WriteFailures++
			h.setAvailable(false)
		})
		// Never log SQL parameters, connection URLs, or driver error messages.
		slog.Warn("decision history write failed", "error_type", fmt.Sprintf("%T", err))
		return retry.RetryableError(err)
	})
	switch {
	case err == nil:
		l.tally(func(h *Health) {
			h.setAvailable(true)
			h.Acknowledged += len(current)
		})
	case ctx.Err() != nil:
		l.tally(func(h *Health) { h.DroppedShutdown += len(current) })
	default:
		l.tally(func(h *Health) { h.DroppedUnavailable += len(current) })
		slog.Warn("decision history dropped: retry budget exhausted", "records", len(current))
	}
}

func (l *Log) cleanup(ctx context.Context) {
	if err := l.store.Cleanup(ctx); err != nil && ctx.Err() == nil {
		l.tally(func(h *Health) { h.CleanupFailures++ })
		slog.Warn("decision history cleanup failed", "error_type", fmt.Sprintf("%T", err))
	}
}
