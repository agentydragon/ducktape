package history

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/google/uuid"
)

// fakeStore is a batchStore whose Append runs `appendFunc`.
type fakeStore struct {
	mu         sync.Mutex
	appendFunc func(ctx context.Context, records []decision.Record) error
	batches    [][]decision.Record
	cleanupErr error
}

func (f *fakeStore) Append(ctx context.Context, records []decision.Record) error {
	f.mu.Lock()
	f.batches = append(f.batches, slices.Clone(records))
	f.mu.Unlock()
	return f.appendFunc(ctx, records)
}

func (f *fakeStore) Cleanup(context.Context) error { return f.cleanupErr }

func startLog(t *testing.T, store batchStore, config LogConfig) *Log {
	t.Helper()
	l, err := newLog(store, retention, config, fastRetries)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.Close)
	return l
}

func succeed(context.Context, []decision.Record) error { return nil }

func TestRecordNeverWaitsOnTheDatabaseAndShutdownCountsWhatWasNotWritten(t *testing.T) {
	entered := make(chan struct{})
	var once sync.Once
	store := &fakeStore{appendFunc: func(ctx context.Context, _ []decision.Record) error {
		once.Do(func() { close(entered) })
		<-ctx.Done() // the database never answers
		return ctx.Err()
	}}
	l := startLog(t, store, LogConfig{QueueSize: 1, BatchSize: 100, FlushTimeout: 10 * time.Millisecond})

	l.Record(testRecord(time.Now()))
	<-entered // the writer holds the first record, blocked
	l.Record(testRecord(time.Now()))
	l.Record(testRecord(time.Now())) // the queue holds one

	health := l.Health()
	if health.Accepted != 2 || health.DroppedOverflow != 1 || health.Queued != 1 || !health.WriterRunning {
		t.Fatalf("while the database is blocked: %+v", health)
	}

	l.Close()
	health = l.Health()
	if health.DroppedShutdown != 2 || health.Queued != 0 || health.WriterRunning {
		t.Fatalf("after close, the in-flight and the queued record are dropped: %+v", health)
	}
	l.Record(testRecord(time.Now()))
	if after := l.Health(); after.Accepted != 2 || after.DroppedShutdown != 3 {
		t.Fatalf("a record after close is dropped, not queued: %+v", after)
	}
}

// A decision is logged by its values: Reason is a pointer, which a handler would otherwise print as
// an address.
func TestADecisionIsLoggedByItsValues(t *testing.T) {
	var logged bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logged, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	reason := decision.NoRule
	record := testRecord(time.Now())
	record.Outcome, record.Reason = decision.Deny, &reason
	l := startLog(t, &fakeStore{appendFunc: func(context.Context, []decision.Record) error { return nil }}, testLogConfig())

	l.Record(record)

	var line struct {
		Decision struct {
			Outcome, Reason, Host, Subject string
			Port                           int
		}
	}
	if err := json.NewDecoder(&logged).Decode(&line); err != nil {
		t.Fatal(err)
	}
	if line.Decision.Outcome != string(decision.Deny) || line.Decision.Reason != string(reason) ||
		line.Decision.Host != record.Host || line.Decision.Port != record.Port ||
		line.Decision.Subject != record.Subject.Namespace+"/"+record.Subject.Name {
		t.Fatalf("logged %+v", line)
	}
}

func TestWriteRetriesThenDropsWithinTheBudget(t *testing.T) {
	store := &fakeStore{appendFunc: func(context.Context, []decision.Record) error { return errors.New("test unavailable") }}
	l := startLog(t, store, testLogConfig())

	l.Record(testRecord(time.Now()))
	flush(t, l)

	health := l.Health()
	if health.WriteFailures != writeAttempts || health.DroppedUnavailable != 1 || health.Acknowledged != 0 ||
		health.Available == nil || *health.Available {
		t.Fatalf("exhausted retries: %+v", health)
	}
}

func TestWriteRecoversAfterAFailure(t *testing.T) {
	failures := 1
	store := &fakeStore{appendFunc: func(context.Context, []decision.Record) error {
		if failures > 0 {
			failures--
			return errors.New("test unavailable")
		}
		return nil
	}}
	l := startLog(t, store, testLogConfig())

	l.Record(testRecord(time.Now()))
	flush(t, l)

	health := l.Health()
	if health.WriteFailures != 1 || health.Acknowledged != 1 || health.DroppedUnavailable != 0 ||
		health.Available == nil || !*health.Available {
		t.Fatalf("one retry succeeds: %+v", health)
	}
}

func TestRecordsOlderThanRetentionAreNotWritten(t *testing.T) {
	store := &fakeStore{appendFunc: succeed}
	l := startLog(t, store, testLogConfig())
	current := testRecord(time.Now())

	l.Record(testRecord(time.Now().Add(-retention - time.Hour)))
	l.Record(current)
	flush(t, l)

	l.Close()
	var written []decision.Record
	for _, batch := range store.batches {
		written = append(written, batch...)
	}
	if len(written) != 1 || written[0].EventID != current.EventID || l.Health().DroppedExpired != 1 {
		t.Fatalf("written %v, health %+v", written, l.Health())
	}
}

func TestBatchesAreBoundedAndKeepOrder(t *testing.T) {
	release := make(chan struct{})
	entered := make(chan struct{})
	var once sync.Once
	store := &fakeStore{appendFunc: func(context.Context, []decision.Record) error {
		once.Do(func() { close(entered) })
		<-release
		return nil
	}}
	l := startLog(t, store, LogConfig{QueueSize: 10, BatchSize: 2, FlushTimeout: 5 * time.Second})
	var recorded []uuid.UUID
	for range 5 {
		r := testRecord(time.Now())
		recorded = append(recorded, r.EventID)
		l.Record(r)
		if len(recorded) == 1 {
			<-entered // the first batch is in flight; the rest queue behind it
		}
	}
	close(release)
	flush(t, l)

	l.Close()
	var written []uuid.UUID
	for _, batch := range store.batches {
		if len(batch) > 2 {
			t.Fatalf("batch of %d exceeds the batch size", len(batch))
		}
		for _, r := range batch {
			written = append(written, r.EventID)
		}
	}
	if !slices.Equal(written, recorded) {
		t.Fatalf("written %v, recorded %v", written, recorded)
	}
}

func TestCleanupFailureAfterABatchIsCounted(t *testing.T) {
	store := &fakeStore{appendFunc: succeed, cleanupErr: errors.New("test cleanup failure")}
	l := startLog(t, store, testLogConfig())

	// The writer collects the second record only after the first batch's cleanup has finished.
	l.Record(testRecord(time.Now()))
	flush(t, l)
	l.Record(testRecord(time.Now()))
	flush(t, l)

	if health := l.Health(); health.CleanupFailures < 1 || health.Acknowledged != 2 {
		t.Fatalf("health %+v", health)
	}
}

func TestLogRejectsNonPositiveBounds(t *testing.T) {
	for _, config := range []LogConfig{{QueueSize: 0, BatchSize: 1}, {QueueSize: 1, BatchSize: 0}} {
		if _, err := newLog(&fakeStore{appendFunc: succeed}, retention, config, fastRetries); err == nil {
			t.Fatalf("%+v was accepted", config)
		}
	}
}
