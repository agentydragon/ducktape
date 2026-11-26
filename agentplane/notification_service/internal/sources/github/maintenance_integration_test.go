package github

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

func TestCleanupUsesConfiguredGitHubRetentionWithoutEnablingProvider(t *testing.T) {
	h := newHarness(t)
	maintenance := Maintenance{Fanout: h.provider.fanout, Retention: settings.Retention{Days: new(3), PayloadDays: new(3)}}
	repositoryID := seedMergeabilityHistory(t, h)
	h.exec(`INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, event, digest, payload, received_at)
		VALUES (42, '00000000-0000-0000-0000-000000000001', 42, 'issue_comment', 'digest', '{}', now() - interval '4 days'),
		(42, '00000000-0000-0000-0000-000000000002', 42, 'issue_comment', 'digest', '{}', now() - interval '2 days')`)
	if err := maintenance.Cleanup(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_delivery WHERE app_id = 42`); count != 1 {
		t.Fatalf("retained %d receipts, want only the two-day-old receipt", count)
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_mergeability_event WHERE repository_id = $1`, repositoryID); count != 0 {
		t.Fatalf("retained %d expired mergeability transitions, want none", count)
	}
	var snapshotState string
	if err := h.db.QueryRowContext(t.Context(), `SELECT state FROM github_pull_request WHERE repository_id = $1`, repositoryID).Scan(&snapshotState); err != nil {
		t.Fatal(err)
	}
	if snapshotState != "OPEN" {
		t.Fatalf("pull request snapshot state = %q, want OPEN after transition cleanup", snapshotState)
	}
	h.exec(`DELETE FROM github_delivery WHERE app_id = 42`)
}

func TestPayloadRetentionExpiresBodiesBeforeFactReceipts(t *testing.T) {
	h := newHarness(t)
	repositoryID := seedMergeabilityHistory(t, h)
	maintenance := Maintenance{Fanout: h.provider.fanout, Retention: settings.Retention{Days: new(7), PayloadDays: new(3)}}
	h.exec(`INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, repository_id, event, digest, payload, fact, received_at)
		VALUES
		(44, '00000000-0000-0000-0000-000000000041', 44, $1, 'issue_comment', 'old-digest', '{"comment":"archive"}', '{"action":"created"}', now() - interval '4 days'),
		(44, '00000000-0000-0000-0000-000000000042', 44, $1, 'issue_comment', 'new-digest', '{"comment":"recent"}', '{"action":"created"}', now() - interval '2 days')`, repositoryID)
	if err := maintenance.Cleanup(t.Context()); err != nil {
		t.Fatal(err)
	}
	var oldBodyExpired bool
	var oldFact []byte
	if err := h.db.QueryRowContext(t.Context(), `SELECT payload IS NULL, fact FROM github_delivery WHERE app_id = 44 AND delivery_id = '00000000-0000-0000-0000-000000000041'`).
		Scan(&oldBodyExpired, &oldFact); err != nil {
		t.Fatal(err)
	}
	var fact struct {
		Action string `json:"action"`
	}
	if err := json.Unmarshal(oldFact, &fact); err != nil {
		t.Fatal(err)
	}
	if !oldBodyExpired || fact.Action != "created" {
		t.Fatalf("old body expired = %v, fact = %s", oldBodyExpired, oldFact)
	}
	var recentBodyPresent bool
	if err := h.db.QueryRowContext(t.Context(), `SELECT payload IS NOT NULL FROM github_delivery WHERE app_id = 44 AND delivery_id = '00000000-0000-0000-0000-000000000042'`).
		Scan(&recentBodyPresent); err != nil {
		t.Fatal(err)
	}
	if !recentBodyPresent {
		t.Fatal("body younger than its retention window expired")
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_delivery WHERE app_id = 44`); count != 2 {
		t.Fatalf("retained %d fact receipts, want both", count)
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_mergeability_event WHERE repository_id = $1`, repositoryID); count != 1 {
		t.Fatalf("retained %d mergeability transitions, want the seven-day fact lifetime", count)
	}
	h.exec(`DELETE FROM github_delivery WHERE app_id = 44`)
}

func TestInfiniteGitHubRetentionStillCleansUpInboxPayloads(t *testing.T) {
	h := newHarness(t)
	maintenance := Maintenance{Fanout: h.provider.fanout, Retention: settings.Retention{Days: nil}}
	repositoryID := seedMergeabilityHistory(t, h)
	sub := h.follow(pullRequestSource(7), "retention")
	h.exec(`INSERT INTO entry (
		inbox_id, cursor, payload, identity_provider, github_app_id, github_delivery_id,
		github_repository_id, github_event, created_at
	) VALUES (
		$1, 1, '{}', 'github', $2, $3, $4, 'issue_comment',
		now() - interval '31 days'
	)`, sub.InboxID, 43, "00000000-0000-0000-0000-000000000003", repositoryID)
	h.exec(`UPDATE inbox SET last_cursor = 1, acknowledged = 1, next_attempt = now() + interval '1 hour' WHERE id = $1`, sub.InboxID)
	h.exec(`INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, event, digest, payload, received_at)
		VALUES (43, '00000000-0000-0000-0000-000000000003', 43, 'issue_comment', 'digest', '{}', now() - interval '4000 days')`)
	if err := maintenance.Cleanup(t.Context()); err != nil {
		t.Fatal(err)
	}
	if _, err := h.store.Cleanup(t.Context(), nil); err != nil {
		t.Fatal(err)
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_delivery WHERE app_id = 43`); count != 1 {
		t.Fatalf("retained %d raw receipts, want 1", count)
	}
	if count := postgres.Count(t, h.db, `SELECT count(*) FROM github_mergeability_event WHERE repository_id = $1`, repositoryID); count != 1 {
		t.Fatalf("retained %d mergeability transitions with infinite retention, want 1", count)
	}
	var expired bool
	if err := h.db.QueryRowContext(t.Context(), `SELECT payload IS NULL FROM entry WHERE inbox_id = $1 AND cursor = 1`, sub.InboxID).Scan(&expired); err != nil {
		t.Fatal(err)
	}
	if !expired {
		t.Fatal("inbox payload did not expire")
	}
	h.exec(`DELETE FROM github_delivery WHERE app_id = 43`)
}

func seedMergeabilityHistory(t *testing.T, h *harness) int64 {
	t.Helper()
	repositoryID := time.Now().UnixNano()
	t.Cleanup(func() {
		for _, query := range []string{
			`DELETE FROM github_mergeability_event WHERE repository_id = $1`,
			`DELETE FROM github_pull_request WHERE repository_id = $1`,
			`DELETE FROM github_repository WHERE repository_id = $1`,
		} {
			if _, err := h.db.ExecContext(context.Background(), query, repositoryID); err != nil {
				t.Errorf("cleanup retention fixture: %v", err)
			}
		}
	})
	h.exec(`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, 'owner/retention')`, repositoryID)
	h.exec(`INSERT INTO github_pull_request
		(repository_id, number, base_ref, head_sha, base_sha, state, mergeability,
		 last_settled_mergeability, snapshot_version, observed_at)
		VALUES ($1, 7, 'main', repeat('a', 40), repeat('b', 40), 'OPEN', 'CONFLICTING', 'CONFLICTING', 1, now())`, repositoryID)
	h.exec(`INSERT INTO github_mergeability_event
		(app_id, installation_id, repository_id, pull_request_number, payload, created_at)
		VALUES (42, 42, $1, 7, '{"previousMergeability":"UNKNOWN","mergeability":"CONFLICTING"}',
			now() - interval '4 days')`, repositoryID)
	return repositoryID
}
