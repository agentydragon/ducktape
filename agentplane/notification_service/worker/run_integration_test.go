package worker

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

// awaitBound only fails a test whose condition never comes.
const awaitBound = 30 * time.Second

// startRun runs the worker until the returned stop is called, which returns what Run returned. A test that
// fails first still ends the run before its database goes.
func startRun(t *testing.T, w *Worker, changes <-chan struct{}) (stop func() error) {
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
	stop := startRun(t, h.worker, nil)
	requireEqual(t, h.awaitReconcile(t).ID, subscription.ID)
	if err := stop(); err != nil {
		t.Fatal(err)
	}
}

func TestRunReadsTheRowsAgainOnceWorkIsSignalled(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	h.exec(t, `UPDATE inbox SET next_attempt = now() + interval '1 hour' WHERE id = $1`, subscription.InboxID)
	changes := make(chan struct{})
	stop := startRun(t, h.worker, changes)

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
	claim := h.claim(t)
	h.exec(t, `UPDATE inbox SET claim_until = now() + interval '300 milliseconds' WHERE id = $1`, claim.ID)

	stop := startRun(t, h.worker, nil)

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

	err := h.worker.Run(ctx, nil)

	var unclassified *UnclassifiedProviderError
	if !errors.As(err, &unclassified) || !errors.Is(err, bug) {
		t.Fatalf("Run = %v, want the Provider's error", err)
	}
}

func TestCleanupUsesConfiguredGitHubRetentionWithoutEnablingProvider(t *testing.T) {
	settings := DefaultSettings()
	settings.GitHubDeliveryRetentionDays = new(3)
	h := newHarness(t, settings)
	h.exec(t, `INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, event, digest, payload, received_at)
		VALUES (42, '00000000-0000-0000-0000-000000000001', 42, 'issue_comment', 'digest', '{}', now() - interval '4 days'),
		(42, '00000000-0000-0000-0000-000000000002', 42, 'issue_comment', 'digest', '{}', now() - interval '2 days')`)
	if _, _, err := h.worker.advance(t.Context(), &retention{}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := h.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_delivery WHERE app_id = 42`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("retained %d receipts, want only the two-day-old receipt", count)
	}
	h.exec(t, `DELETE FROM github_delivery WHERE app_id = 42`)
}

func TestInfiniteGitHubRetentionStillCleansUpInboxPayloads(t *testing.T) {
	settings := DefaultSettings()
	settings.GitHubDeliveryRetentionDays = nil
	h := newHarness(t, settings)
	sub := h.subscribe(t, "retention")
	h.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
		VALUES ($1, 1, '{"provider":"github"}', '{}', now() - interval '31 days')`, sub.InboxID)
	h.exec(t, `UPDATE inbox SET last_cursor = 1, next_attempt = now() + interval '1 hour' WHERE id = $1`, sub.InboxID)
	h.exec(t, `INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, event, digest, payload, received_at)
		VALUES (43, '00000000-0000-0000-0000-000000000003', 43, 'issue_comment', 'digest', '{}', now() - interval '4000 days')`)
	if _, _, err := h.worker.advance(t.Context(), &retention{}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := h.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_delivery WHERE app_id = 43`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("retained %d raw receipts, want 1", count)
	}
	var expired bool
	if err := h.db.QueryRowContext(t.Context(), `SELECT payload IS NULL FROM entry WHERE inbox_id = $1 AND cursor = 1`, sub.InboxID).Scan(&expired); err != nil {
		t.Fatal(err)
	}
	if !expired {
		t.Fatal("inbox payload did not expire")
	}
	h.exec(t, `DELETE FROM github_delivery WHERE app_id = 43`)
}
