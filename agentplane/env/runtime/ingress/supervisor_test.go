package ingress

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
)

func TestSupervisorStartsOneWorkerPerUnsealedSessionAndJoinsOnCancel(t *testing.T) {
	waitCtx, timeout := context.WithTimeout(context.Background(), 5*time.Second)
	defer timeout()
	runCtx, cancel := context.WithCancel(waitCtx)
	defer cancel()
	store := newTestStore(t)
	sealed := Source{ID: "sealed-session"}
	if err := store.WriteAcknowledgement(sealed.ID, retained.Acknowledgement{Sealed: true}); err != nil {
		t.Fatal(err)
	}
	active := Source{ID: "active-session"}

	var changeMu sync.Mutex
	changed := make(chan struct{})
	changes := func() <-chan struct{} {
		changeMu.Lock()
		defer changeMu.Unlock()
		return changed
	}
	signalChange := func() {
		changeMu.Lock()
		old := changed
		changed = make(chan struct{})
		close(old)
		changeMu.Unlock()
	}
	secondScan := make(chan struct{})
	var inventoryScans int
	inventory := func() []Source {
		inventoryScans++
		if inventoryScans == 2 {
			close(secondScan)
		}
		return []Source{active, active, sealed}
	}
	supervisor := NewSupervisor(SupervisorConfig{Store: store, Inventory: inventory, InventoryChanges: changes})
	started := make(chan string, 8)
	exiting := make(chan string, 8)
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseWorkers := func() { releaseOnce.Do(func() { close(release) }) }
	defer releaseWorkers()
	var callsMu sync.Mutex
	calls := make(map[string]int)
	deliver := func(ctx context.Context, source Source) error {
		callsMu.Lock()
		calls[source.ID]++
		callsMu.Unlock()
		started <- source.ID
		<-ctx.Done()
		exiting <- source.ID
		<-release
		return ctx.Err()
	}
	runDone := make(chan error, 1)
	go func() { runDone <- supervisor.supervise(runCtx, deliver) }()

	if id := await(t, waitCtx, started, "the active Session worker"); id != active.ID {
		t.Fatalf("started Session = %q, want %q", id, active.ID)
	}
	signalChange()
	await(t, waitCtx, secondScan, "a second discovery pass")
	cancel()
	await(t, waitCtx, exiting, "the worker to observe cancellation")
	select {
	case err := <-runDone:
		t.Fatalf("supervisor returned before joining its worker: %v", err)
	default:
	}
	releaseWorkers()
	if err := await(t, waitCtx, runDone, "worker cancellation and join"); err != nil {
		t.Fatalf("supervisor shutdown: %v", err)
	}

	callsMu.Lock()
	defer callsMu.Unlock()
	if calls[active.ID] != 1 {
		t.Fatalf("workers for unsealed Session = %d, want 1", calls[active.ID])
	}
	if calls[sealed.ID] != 0 {
		t.Fatalf("workers for sealed Session = %d, want 0", calls[sealed.ID])
	}
}

func TestSupervisorReportsDeliveryFailureAndRetriesUntilCancel(t *testing.T) {
	waitCtx, timeout := context.WithTimeout(context.Background(), 5*time.Second)
	defer timeout()
	runCtx, cancel := context.WithCancel(waitCtx)
	defer cancel()
	store := newTestStore(t)
	wantErr := errors.New("acknowledgement publication failed")
	failure := make(chan error, 1)
	started := make(chan int, 2)
	supervisor := NewSupervisor(SupervisorConfig{
		Store:            store,
		Inventory:        func() []Source { return []Source{{ID: "failure-session"}} },
		InventoryChanges: func() <-chan struct{} { return make(chan struct{}) },
		OnWorkerFailure:  func(err error) { failure <- err },
	})
	var calls atomic.Int32
	deliver := func(ctx context.Context, _ Source) error {
		call := int(calls.Add(1))
		started <- call
		if call == 1 {
			return wantErr
		}
		<-ctx.Done()
		return ctx.Err()
	}
	runDone := make(chan error, 1)
	go func() { runDone <- supervisor.supervise(runCtx, deliver) }()
	if call := await(t, waitCtx, started, "the initial delivery worker"); call != 1 {
		t.Fatalf("delivery call = %d, want 1", call)
	}
	if err := await(t, waitCtx, failure, "delivery failure propagation"); !errors.Is(err, wantErr) {
		t.Fatalf("reported delivery error = %v, want %v", err, wantErr)
	}
	if call := await(t, waitCtx, started, "retry after delivery failure"); call != 2 {
		t.Fatalf("retry call = %d, want 2", call)
	}
	cancel()
	if err := await(t, waitCtx, runDone, "worker cancellation and join"); err != nil {
		t.Fatalf("supervisor shutdown: %v", err)
	}
}

func TestSupervisorReportsFailureBeforeBlockedSiblingJoinsAndIgnoresCancellation(t *testing.T) {
	waitCtx, timeout := context.WithTimeout(context.Background(), 5*time.Second)
	defer timeout()
	runCtx, cancel := context.WithCancel(waitCtx)
	defer cancel()
	blocked := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	wantErr := errors.New("local Session delivery failed")
	failures := make(chan error, 2)
	supervisor := NewSupervisor(SupervisorConfig{
		Store: newTestStore(t),
		Inventory: func() []Source {
			return []Source{{ID: "failing-session"}, {ID: "blocked-session"}}
		},
		InventoryChanges: func() <-chan struct{} { return make(chan struct{}) },
		OnWorkerFailure: func(err error) {
			failures <- err
			cancel()
		},
	})
	deliver := func(ctx context.Context, source Source) error {
		if source.ID == "failing-session" {
			<-blocked
			return wantErr
		}
		close(blocked)
		<-release
		return ctx.Err()
	}
	runDone := make(chan error, 1)
	go func() { runDone <- supervisor.supervise(runCtx, deliver) }()
	if err := await(t, waitCtx, failures, "failure while sibling delivery is blocked"); !errors.Is(err, wantErr) {
		t.Fatalf("reported worker failure = %v, want %v", err, wantErr)
	}
	select {
	case err := <-runDone:
		t.Fatalf("supervisor joined blocked sibling before failure callback: %v", err)
	default:
	}
	cancel()
	releaseOnce.Do(func() { close(release) })
	if err := await(t, waitCtx, runDone, "canceled sibling join"); err != nil {
		t.Fatalf("supervisor cancellation = %v", err)
	}
	select {
	case err := <-failures:
		t.Fatalf("ordinary cancellation reported as worker failure: %v", err)
	default:
	}
}

func TestSupervisorDiscoversAddedAndReopenedSessions(t *testing.T) {
	waitCtx, timeout := context.WithTimeout(context.Background(), 5*time.Second)
	defer timeout()
	runCtx, cancel := context.WithCancel(waitCtx)
	defer cancel()
	store := newTestStore(t)
	reopened := Source{ID: "reopened-session"}
	if err := store.WriteAcknowledgement(reopened.ID, retained.Acknowledgement{Sealed: true}); err != nil {
		t.Fatal(err)
	}
	added := Source{ID: "added-session"}

	var mu sync.Mutex
	changed := make(chan struct{})
	var inventory []Source
	var scans int
	changes := func() <-chan struct{} {
		mu.Lock()
		defer mu.Unlock()
		return changed
	}
	signalChange := func() {
		mu.Lock()
		old := changed
		changed = make(chan struct{})
		close(old)
		mu.Unlock()
	}
	inventory = []Source{reopened}
	snapshot := func() []Source {
		mu.Lock()
		current := append([]Source(nil), inventory...)
		scans++
		first := scans == 1
		mu.Unlock()
		if first {
			// Change membership after the snapshot but before the supervisor starts
			// waiting. Its already-subscribed channel must retain this wakeup.
			mu.Lock()
			inventory = append(inventory, added)
			mu.Unlock()
			signalChange()
		}
		return current
	}
	supervisor := NewSupervisor(SupervisorConfig{
		Store: store, Inventory: snapshot, InventoryChanges: changes,
		OnWorkerFailure: func(err error) { t.Errorf("delivery failed: %v", err) },
	})
	started := make(chan string, 4)
	deliver := func(ctx context.Context, source Source) error {
		started <- source.ID
		<-ctx.Done()
		return ctx.Err()
	}
	runDone := make(chan error, 1)
	go func() { runDone <- supervisor.supervise(runCtx, deliver) }()

	if id := await(t, waitCtx, started, "the newly added Session worker"); id != added.ID {
		t.Fatalf("started Session = %q, want %q", id, added.ID)
	}
	if err := store.WriteAcknowledgement(reopened.ID, retained.Acknowledgement{}); err != nil {
		t.Fatal(err)
	}
	signalChange()
	if id := await(t, waitCtx, started, "the reopened Session worker"); id != reopened.ID {
		t.Fatalf("started Session = %q, want %q", id, reopened.ID)
	}

	cancel()
	if err := await(t, waitCtx, runDone, "worker cancellation and join"); err != nil {
		t.Fatalf("supervisor shutdown: %v", err)
	}
}
