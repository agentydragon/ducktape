package worker

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
)

// awaitBound only fails a test whose condition never comes.
const awaitBound = 30 * time.Second

// startRun runs the worker until the returned stop is called, which returns what Run returned. A test that
// fails first still ends the run before its database goes.
func startRun(t *testing.T, w interface {
	Run(context.Context, <-chan struct{}) error
}, changes <-chan struct{},
) (stop func() error) {
	t.Helper()
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- w.Run(ctx, changes) }()
	var once sync.Once
	var result error
	stop = func() error {
		once.Do(func() {
			cancel()
			select {
			case result = <-done:
			case <-time.After(awaitBound):
				result = errors.New("Run did not return after its context ended")
			}
		})
		return result
	}
	t.Cleanup(func() { _ = stop() })
	return stop
}

func (h *harness) awaitReconcile(t *testing.T) store.DueSubscription {
	t.Helper()
	select {
	case due := <-h.actions.reconciled:
		return due
	case <-time.After(awaitBound):
		t.Fatal("no subscription was read")
		return store.DueSubscription{}
	}
}

func TestRunWorksInboxesCommittedBeforeItStarted(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")

	// No wakeup is ever sent: work committed before the listener existed is found in the rows.
	stop := startRun(t, h.sourceWorker, nil)
	requireEqual(t, h.awaitReconcile(t).ID, subscription.ID)
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

func TestRunReadsTheRowsAgainOnceWorkIsSignalled(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	h.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, subscription.ID)
	changes := make(chan struct{})
	stop := startRun(t, h.sourceWorker, changes)

	// Renewing commits a subscription that is due now. Run has nothing to do before the signal, which an
	// unbuffered channel only accepts once Run is waiting for it, or about to read the rows anyway.
	if _, err := h.store.Renew(t.Context(), testIncarnation, subscription.ID, subscription.Version, 7); err != nil {
		t.Fatal(err)
	}
	select {
	case changes <- struct{}{}:
	case <-time.After(awaitBound):
		t.Fatal("Run did not take the signal")
	}

	requireEqual(t, h.awaitReconcile(t).ID, subscription.ID)
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

func TestRunReclaimsAnInboxWhoseWorkerDiedHoldingTheLease(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	// A worker claimed the inbox and was never heard from again: what it left is a lease deadline.
	claim := h.sourceClaim(t)
	h.exec(t, `UPDATE inbox SET claim_until = now() + interval '300 milliseconds' WHERE id = $1`, claim.ID)

	stop := startRun(t, h.sourceWorker, nil)

	requireEqual(t, h.awaitReconcile(t).ID, subscription.ID)
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

func TestRunReturnsAProviderErrorOutsideItsContract(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	h.subscribe(t, "first")
	bug := errors.New("provider bug")
	h.actions.returns(bug)
	ctx, cancel := context.WithTimeout(t.Context(), awaitBound)
	defer cancel()

	err := h.sourceWorker.Run(ctx, nil)

	var unclassified *sources.UnclassifiedProviderError
	if !errors.As(err, &unclassified) || !errors.Is(err, bug) {
		t.Fatalf("Run = %v, want the Provider's error", err)
	}
}

func TestRunKeepsGenericInboxRetentionScheduled(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	h.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	h.exec(t, `UPDATE inbox SET acknowledged = 1, next_attempt = now() + interval '1 hour' WHERE id = $1`, subscription.InboxID)

	stop := startRun(t, h.worker, nil)
	defer stop()

	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	timer := time.NewTimer(awaitBound)
	defer timer.Stop()
	for {
		var expired bool
		if err := h.db.QueryRowContext(t.Context(), `SELECT payload IS NULL FROM entry WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID).Scan(&expired); err != nil {
			t.Fatal(err)
		}
		if expired {
			return
		}
		select {
		case <-ticker.C:
		case <-timer.C:
			t.Fatal("delivery Run did not clean expired inbox payloads")
		}
	}
}
