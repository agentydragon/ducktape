package operation

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestCompletionPublicationPrecedesSingleConsumption(t *testing.T) {
	ctx := context.Background()
	var mu sync.Mutex
	var slot *Operation
	var workers sync.WaitGroup
	publicationStarted := make(chan struct{})
	releasePublication := make(chan struct{})
	var executions atomic.Int32
	op := New(ctx)
	mu.Lock()
	slot = op
	mu.Unlock()

	op.Register(&workers)
	if err := op.Start(&mu, func(done bool) error {
		if done {
			close(publicationStarted)
			<-releasePublication
		}
		return nil
	}, func(err error) { t.Errorf("unexpected publication error: %v", err) }, func(context.Context) error {
		executions.Add(1)
		return nil
	}, nil, nil); err != nil {
		t.Fatal(err)
	}
	<-publicationStarted
	consumedResult := make(chan bool, 1)
	consumerStarted := make(chan struct{})
	go func() {
		close(consumerStarted)
		_, consumed := Consume(&mu, &slot)
		consumedResult <- consumed
	}()
	<-consumerStarted
	premature := false
	select {
	case <-consumedResult:
		premature = true
	case <-time.After(50 * time.Millisecond):
	}
	if premature {
		close(releasePublication)
		workers.Wait()
		t.Fatal("consumer returned before completion publication finished")
	}
	if op.Finished() {
		t.Fatal("operation became finished before completion publication finished")
	}
	close(releasePublication)
	workers.Wait()

	if !<-consumedResult {
		t.Fatal("consumer did not consume after completion publication finished")
	}
	mu.Lock()
	remaining := slot
	mu.Unlock()
	if remaining != nil {
		t.Fatalf("slot remained set after consume: %p", remaining)
	}
	if _, ok := Consume(&mu, &slot); ok {
		t.Fatal("operation was consumed more than once")
	}
	if got := executions.Load(); got != 1 {
		t.Fatalf("work executions = %d, want 1", got)
	}
}

func TestInitialPublicationFailureDoesNotRunWork(t *testing.T) {
	publicationErr := errors.New("state unavailable")
	var workers sync.WaitGroup
	var executions atomic.Int32
	var reported error
	op := New(context.Background())
	var ownerMu sync.Mutex
	op.Register(&workers)
	err := op.Start(&ownerMu, func(done bool) error {
		if done {
			t.Fatal("completion was published after initial publication failed")
		}
		return publicationErr
	}, func(err error) { reported = err }, func(context.Context) error {
		executions.Add(1)
		return nil
	}, nil, nil)
	workers.Wait()
	if !errors.Is(err, publicationErr) || !errors.Is(reported, publicationErr) || !errors.Is(op.Err(), publicationErr) {
		t.Fatalf("initial publication error not retained: start=%v reported=%v result=%v", err, reported, op.Err())
	}
	if !op.Finished() {
		t.Fatal("operation did not settle after initial publication failed")
	}
	if got := executions.Load(); got != 0 {
		t.Fatalf("work executions = %d, want 0", got)
	}
}

func TestCompletionPublicationFailureSettlesWorkerAndRetainsErrors(t *testing.T) {
	publicationErr := errors.New("completion state write failed")
	workErr := errors.New("native effect failed")
	reportErr := make(chan error, 1)
	onDoneEntered := make(chan struct{})
	releaseOnDone := make(chan struct{})
	var workers sync.WaitGroup
	var executions atomic.Int32
	op := New(context.Background())
	var ownerMu sync.Mutex
	op.Register(&workers)
	if err := op.Start(&ownerMu, func(done bool) error {
		if done {
			return publicationErr
		}
		return nil
	}, func(err error) { reportErr <- err }, func(context.Context) error {
		executions.Add(1)
		return workErr
	}, nil, func() {
		close(onDoneEntered)
		<-releaseOnDone
	}); err != nil {
		t.Fatal(err)
	}
	<-op.Done()
	<-onDoneEntered
	waited := make(chan struct{})
	go func() {
		workers.Wait()
		close(waited)
	}()
	select {
	case <-waited:
		t.Fatal("worker join completed while its completion callback was running")
	default:
	}
	if !op.Finished() || !errors.Is(op.Err(), workErr) || !errors.Is(op.Err(), publicationErr) {
		t.Fatalf("operation did not retain both errors: finished=%t err=%v", op.Finished(), op.Err())
	}
	if got := executions.Load(); got != 1 {
		t.Fatalf("work executions = %d, want 1", got)
	}
	close(releaseOnDone)
	select {
	case <-waited:
	case <-time.After(time.Second):
		t.Fatal("worker join did not settle after completion callback returned")
	}
	if err := <-reportErr; !errors.Is(err, publicationErr) {
		t.Fatalf("reported error = %v, want completion publication error", err)
	}
}

func TestCancelSettlesAndCanBeConsumedOnce(t *testing.T) {
	var mu sync.Mutex
	var slot *Operation
	var workers sync.WaitGroup
	started := make(chan struct{})
	op := New(context.Background())
	mu.Lock()
	slot = op
	mu.Unlock()
	op.Register(&workers)
	if err := op.Start(&mu, func(bool) error { return nil }, func(err error) { t.Errorf("unexpected publication error: %v", err) }, func(ctx context.Context) error {
		close(started)
		<-ctx.Done()
		return ctx.Err()
	}, nil, nil); err != nil {
		t.Fatal(err)
	}
	<-started
	op.Cancel()
	workers.Wait()
	if !errors.Is(op.Err(), context.Canceled) {
		t.Fatalf("operation error = %v, want context cancellation", op.Err())
	}
	if consumed, ok := Consume(&mu, &slot); !ok || consumed != op {
		t.Fatalf("consume after cancellation = (%p, %t), want %p", consumed, ok, op)
	}
	if _, ok := Consume(&mu, &slot); ok {
		t.Fatal("operation was consumed more than once")
	}
}
