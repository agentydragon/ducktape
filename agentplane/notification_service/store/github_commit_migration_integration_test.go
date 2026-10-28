package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"io/fs"
	"math/rand/v2"
	"os"
	"reflect"
	"testing"
	"testing/fstest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/google/uuid"
	"github.com/pressly/goose/v3"
)

func TestGitHubCommitMigrationCopiesLegacyFactsAndRefreshes(t *testing.T) {
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	// This is an isolated clone. Rebuild only the historical migrations here so the fixture exercises the actual
	// v6 -> v7 transition even when the package template has newer storage migrations applied.
	if _, err := db.ExecContext(t.Context(), `DROP SCHEMA public CASCADE`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `CREATE SCHEMA public`); err != nil {
		t.Fatal(err)
	}
	provider := notificationMigrationProvider(t, db)
	if _, err := provider.UpTo(t.Context(), 6); err != nil {
		t.Fatalf("build historical v6 schema: %v", err)
	}

	base := 1<<41 + rand.Int64N(1<<40)
	ref := model.DestinationRef{Namespace: "test-namespace", Name: "migration-sandbox", UID: testSandboxUID}
	owner := newOwner()
	sessionID := "migration-session"
	source := model.GitHubSource{Repository: testRepositoryName, Subject: model.PullRequestSubject{Number: 7}}
	subject := GitHubSubjectKey{RepositoryID: base + 2, Kind: "pull_request", Key: "7"}
	inboxID := insertHistoricalInbox(t, db, owner, ref, sessionID)
	g := githubFixture{
		fixture:      fixture{db: db},
		app:          base,
		installation: base + 1,
		repository:   base + 2,
		destination:  ref,
		inbox:        inboxID,
	}
	now := time.Now().UTC()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_installation (app_id, installation_id, generation)
		VALUES ($1, $2, 5)`, g.app, g.installation); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2)`,
		g.repository, testRepositoryName); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_repository_access
		(app_id, installation_id, repository_id, generation, validated_installation_generation,
		 checked_at, valid_until, last_success_at)
		VALUES ($1, $2, $3, 4, 5, $4, $5, $4)`,
		g.app, g.installation, g.repository, now, now.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_subject
		(repository_id, kind, subject_key, generation, last_success_at)
		VALUES ($1, 'pull_request', '7', 3, $2)`, g.repository, now); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_subject_revision
		(repository_id, kind, subject_key, head_repository_id, sha)
		VALUES ($1, 'pull_request', '7', $1, $2)`, g.repository, commitA); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_pull_request
		(repository_id, number, base_ref, head_sha, base_sha, state, mergeability, last_settled_mergeability,
		 snapshot_version, observed_at)
		VALUES ($1, 7, 'main', $2, $3, 'OPEN', 'UNKNOWN', 'UNKNOWN', 1, $4)`,
		g.repository, commitA, commitB, now); err != nil {
		t.Fatal(err)
	}
	subscriptionID := insertHistoricalSubscription(t, db, inboxID, ref, sessionID, "migration-subscription", source,
		g.app, g.installation, g.repository, 0)
	sub := githubSubscription{ID: subscriptionID, InboxID: inboxID, Source: source, Subject: subject}
	deliveryID := uuid.New()
	deliveryDigest := sha256.Sum256(deliveryID[:])
	var deliveryPosition int64
	if err := db.QueryRowContext(t.Context(), `INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, repository_id, event, action, head_sha, digest, payload)
		VALUES ($1, $2, $3, $4, 'check_run', 'completed', $5, $6, $7::jsonb) RETURNING position`,
		g.app, deliveryID, g.installation, g.repository, commitA, deliveryDigest[:],
		`{"action":"completed","check_run":{"id":303}}`).Scan(&deliveryPosition); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_delivery_subject
		(delivery_position, repository_id, kind, subject_key)
		VALUES ($1, $2, 'pull_request', '7')`, deliveryPosition, g.repository); err != nil {
		t.Fatal(err)
	}

	observedAt := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
	checkPayload := legacyCheckRunPayload(t, g.repository, commitA, 303, 202, observedAt)
	otherAppCheckPayload := legacyCheckRunPayload(t, g.repository, commitA, 302, 203, observedAt)
	statusPayload := legacyCommitStatusPayload(t, g.repository, commitA, 51, "ci/unit", observedAt)
	legacyCreatedAt := observedAt.Add(-time.Hour)
	for _, event := range []struct {
		sequence int64
		name     string
		payload  []byte
	}{
		{37, string(model.EventCheckRun), checkPayload},
		{39, string(model.EventStatus), statusPayload},
	} {
		if _, err := db.ExecContext(t.Context(), `INSERT INTO github_ci_event
			(sequence, app_id, installation_id, repository_id, head_sha, event, payload, created_at)
			OVERRIDING SYSTEM VALUE VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)`,
			event.sequence, g.app, g.installation, g.repository, commitA, event.name, string(event.payload), legacyCreatedAt); err != nil {
			t.Fatal(err)
		}
	}
	for _, current := range []struct {
		entity   string
		sequence int64
		event    string
		payload  []byte
	}{
		{"303", 38, string(model.EventCheckRun), checkPayload},
		{"302", 45, string(model.EventCheckRun), otherAppCheckPayload},
		{"ci/unit", 46, string(model.EventStatus), statusPayload},
	} {
		if _, err := db.ExecContext(t.Context(), `INSERT INTO github_ci_current
			(app_id, installation_id, repository_id, head_sha, event, entity_key, last_sequence, payload)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)`,
			g.app, g.installation, g.repository, commitA, current.event, current.entity, current.sequence, string(current.payload)); err != nil {
			t.Fatal(err)
		}
		if _, err := db.ExecContext(t.Context(), `INSERT INTO github_ci_subscription_snapshot
			(subscription_id, app_id, installation_id, repository_id, event, sequence, payload)
			VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)`,
			sub.ID, g.app, g.installation, g.repository, current.event, current.sequence, string(current.payload)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE subscription SET github_ci_after_sequence = 30 WHERE id = $1`, sub.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE github_delivery SET ci_normalized = true WHERE app_id = $1 AND delivery_id = $2`,
		g.app, deliveryID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO github_ci_refresh
		(app_id, installation_id, repository_id, subject_kind, subject_key, generation,
		 last_success_at, error_kind, error, error_since, error_observed_at, next_attempt, claim, claim_until)
		VALUES ($1, $2, $3, 'pull_request', '7', 9, $4, 'unavailable', 'legacy aggregate failure', $4, $4, $5, $6, $5)`,
		g.app, g.installation, g.repository, observedAt, observedAt.Add(time.Hour), uuid.New()); err != nil {
		t.Fatal(err)
	}
	// Event retention may remove old transitions while a current row or snapshot still refers to a later sequence.
	if _, err := db.ExecContext(t.Context(), `SELECT setval(pg_get_serial_sequence('github_ci_event', 'sequence'), 60, true)`); err != nil {
		t.Fatal(err)
	}

	if _, err := provider.UpTo(t.Context(), 7); err != nil {
		t.Fatalf("apply native observation migration: %v", err)
	}

	for _, identity := range []struct {
		event string
		seq   int64
	}{
		{string(model.EventCheckRun), 37},
		{string(model.EventStatus), 39},
	} {
		var oldPayload, newPayload []byte
		if err := db.QueryRowContext(t.Context(), `SELECT payload FROM github_ci_event WHERE sequence = $1`, identity.seq).Scan(&oldPayload); err != nil {
			t.Fatal(err)
		}
		var sequence int64
		if err := db.QueryRowContext(t.Context(), `SELECT sequence, payload FROM github_commit_event WHERE event = $1 AND sequence = $2`,
			identity.event, identity.seq).Scan(&sequence, &newPayload); err != nil {
			t.Fatal(err)
		}
		if sequence != identity.seq || !jsonEqual(oldPayload, newPayload) {
			t.Errorf("migrated %s event identity/payload = %d/%s, want unchanged sequence %d and payload %s",
				identity.event, sequence, newPayload, identity.seq, oldPayload)
		}
	}
	for _, identity := range []struct {
		event   string
		entity  string
		seq     int64
		payload []byte
	}{
		{string(model.EventCheckRun), "303", 38, checkPayload},
		{string(model.EventCheckRun), "302", 45, otherAppCheckPayload},
		{string(model.EventStatus), "ci/unit", 46, statusPayload},
	} {
		var oldPayload, newPayload []byte
		if err := db.QueryRowContext(t.Context(), `SELECT payload FROM github_ci_current
			WHERE app_id = $1 AND event = $2 AND entity_key = $3`, g.app, identity.event, identity.entity).Scan(&oldPayload); err != nil {
			t.Fatal(err)
		}
		if err := db.QueryRowContext(t.Context(), `SELECT payload FROM github_commit_current
			WHERE app_id = $1 AND event = $2 AND entity_key = $3`, g.app, identity.event, identity.entity).Scan(&newPayload); err != nil {
			t.Fatal(err)
		}
		if !jsonEqual(oldPayload, newPayload) || !jsonEqual(newPayload, identity.payload) {
			t.Errorf("migrated %s current payload = %s, want unchanged legacy payload %s", identity.event, newPayload, oldPayload)
		}
		var oldSnapshot, newSnapshot []byte
		if err := db.QueryRowContext(t.Context(), `SELECT payload FROM github_ci_subscription_snapshot WHERE subscription_id = $1 AND sequence = $2`,
			sub.ID, identity.seq).Scan(&oldSnapshot); err != nil {
			t.Fatal(err)
		}
		if err := db.QueryRowContext(t.Context(), `SELECT payload FROM github_commit_subscription_snapshot WHERE subscription_id = $1 AND sequence = $2`,
			sub.ID, identity.seq).Scan(&newSnapshot); err != nil {
			t.Fatal(err)
		}
		if !jsonEqual(oldSnapshot, newSnapshot) || !jsonEqual(newSnapshot, identity.payload) {
			t.Errorf("migrated %s snapshot payload = %s, want unchanged legacy payload %s", identity.event, newSnapshot, oldSnapshot)
		}
	}
	if got := queryInt64(t, db, `SELECT count(*) FROM github_delivery WHERE app_id = $1 AND delivery_id = $2 AND commit_normalized`, g.app, deliveryID); got != 1 {
		t.Errorf("copied normalized-delivery marker = %d, want one", got)
	}
	var cursor int64
	if err := db.QueryRowContext(t.Context(), `SELECT github_commit_after_sequence FROM subscription WHERE id = $1`, sub.ID).Scan(&cursor); err != nil {
		t.Fatal(err)
	}
	if cursor != 30 {
		t.Errorf("copied subscription cursor = %d, want 30", cursor)
	}
	if got := queryInt64(t, db, `SELECT count(*) FROM github_commit_subscription_snapshot WHERE subscription_id = $1`, sub.ID); got != 3 {
		t.Errorf("copied snapshots = %d, want three exact legacy rows", got)
	}
	if got := queryInt64(t, db, `SELECT count(*) FROM github_commit_refresh WHERE app_id = $1 AND generation = 9 AND last_success_at IS NULL
		AND error IS NULL AND next_attempt IS NULL AND claim IS NULL AND claim_until IS NULL`, g.app); got != 2 {
		t.Errorf("due-now per-category refresh rows = %d, want two", got)
	}

	lateDestination := g.destination
	lateDestination.Name = "migration-late"
	lateInboxID := insertHistoricalInbox(t, db, owner, lateDestination, sessionID)
	lateSubscriptionID := insertHistoricalSubscription(t, db, lateInboxID, lateDestination, sessionID,
		"migration-late-subscription", sub.Source, g.app, g.installation, g.repository, deliveryPosition)
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := snapshotGitHubCommitObservations(t.Context(), tx, lateSubscriptionID, g.app, sub.Subject, sub.Source); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if got := queryInt64(t, db, `SELECT count(*) FROM github_commit_subscription_snapshot WHERE subscription_id = $1 AND event = 'check_run'`, lateSubscriptionID); got != 2 {
		t.Errorf("legacy App-specific check-run snapshots = %d, want latest attempt from each of two Apps", got)
	}

	if changed := observeNativeCheckAndStatusAgain(t, db, g); changed {
		t.Error("an identical native observation appended a transition after migration")
	}
	if got := queryInt64(t, db, `SELECT count(*) FROM github_commit_event WHERE app_id = $1`, g.app); got != 2 {
		t.Errorf("event count after identical native observations = %d, want two original identities", got)
	}

	newRun := checkRunObservation(g, commitA, model.CheckRunQueued, "")
	newRun.ID = 999
	tx, err = db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := observeGitHubCheckRun(t.Context(), tx, g.app, g.installation, newRun, time.Now().UTC()); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	var newest int64
	if err := db.QueryRowContext(t.Context(), `SELECT max(sequence) FROM github_commit_event WHERE app_id = $1`, g.app).Scan(&newest); err != nil {
		t.Fatal(err)
	}
	if newest != 61 {
		t.Errorf("next transition sequence = %d, want 61 above retained/current/snapshot/old-sequence watermark 60", newest)
	}
}

func insertHistoricalInbox(t *testing.T, db *sql.DB, owner model.ServiceAccount, ref model.DestinationRef, sessionID string) uuid.UUID {
	t.Helper()
	inboxID := uuid.New()
	destination, err := json.Marshal(ref)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO inbox
		(id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor, acknowledged,
		 covered, expired_through, retired, updated_at, claim_until)
		VALUES ($1, $2, $3, $4, $5::jsonb, $6, 0, 0, 0, 0, false, $7, $7)`,
		inboxID, owner.Namespace, owner.Name, model.DestinationKey(ref, sessionID), string(destination), sessionID, now); err != nil {
		t.Fatal(err)
	}
	return inboxID
}

func insertHistoricalSubscription(t *testing.T, db *sql.DB, inboxID uuid.UUID, ref model.DestinationRef,
	sessionID, idempotencyKey string, source model.GitHubSource, appID, installationID, repositoryID, startPosition int64,
) uuid.UUID {
	t.Helper()
	creation, err := json.Marshal(struct {
		DestinationRef model.DestinationRef `json:"destination_ref"`
		SessionID      string               `json:"session_id"`
		IdempotencyKey string               `json:"idempotency_key"`
		LifetimeDays   int                  `json:"lifetime_days"`
		Source         struct {
			Provider   string `json:"provider"`
			Repository string `json:"repository"`
			Subject    struct {
				Kind   string `json:"kind"`
				Number int64  `json:"number"`
			} `json:"subject"`
			Events []string `json:"events"`
		} `json:"source"`
	}{
		DestinationRef: ref,
		SessionID:      sessionID,
		IdempotencyKey: idempotencyKey,
		LifetimeDays:   7,
		Source: struct {
			Provider   string `json:"provider"`
			Repository string `json:"repository"`
			Subject    struct {
				Kind   string `json:"kind"`
				Number int64  `json:"number"`
			} `json:"subject"`
			Events []string `json:"events"`
		}{
			Provider:   "github",
			Repository: source.Repository,
			Subject: struct {
				Kind   string `json:"kind"`
				Number int64  `json:"number"`
			}{Kind: "pull_request", Number: 7},
			Events: nil,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	subscriptionID := uuid.New()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO subscription
		(id, inbox_id, idempotency_key, creation, version, actions_after_sequence, cancelled, expires_at, generation,
		 github_start_position, github_app_id, github_installation_id, github_repository_id,
		 github_subject_kind, github_subject_key)
		VALUES ($1, $2, $3, $4::jsonb, 1, NULL, false, now() + interval '7 days', 1, $5, $6, $7, $8,
		 'pull_request', '7')`,
		subscriptionID, inboxID, idempotencyKey, string(creation), startPosition, appID, installationID, repositoryID); err != nil {
		t.Fatal(err)
	}
	return subscriptionID
}

func notificationMigrationProvider(t *testing.T, db *sql.DB) *goose.Provider {
	t.Helper()
	names := []string{
		"00001_baseline.sql",
		"00002_messages.sql",
		"00003_github_delivery_retention.sql",
		"00004_github_mergeability.sql",
		"00005_github_mergeability_freshness.sql",
		"00006_github_ci_recovery.sql",
		"00007_native_github_commit_observations.sql",
		"00008_github_pr_state_recovery.sql",
	}
	files := make(fstest.MapFS, len(names))
	for _, name := range names {
		path, err := runfiles.Rlocation("_main/agentplane/notification_service/migrations/notifications/" + name)
		if err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		files[name] = &fstest.MapFile{Data: data}
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, fs.FS(files),
		goose.WithTableName("notification_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func legacyCheckRunPayload(t *testing.T, repositoryID int64, sha string, id, appID int64, observedAt time.Time) []byte {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"repositoryId": repositoryID, "headSha": sha, "event": "check_run", "action": "completed",
		"id": id, "checkAppId": appID, "name": "unit", "status": "completed", "conclusion": "success",
		"detailsUrl": "https://checks.example/unit", "observedAt": observedAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func legacyCommitStatusPayload(t *testing.T, repositoryID int64, sha string, id int64, context string, observedAt time.Time) []byte {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"repositoryId": repositoryID, "headSha": sha, "event": "status", "id": id,
		"name": context, "status": "success", "detailsUrl": "https://statuses.example/unit", "observedAt": observedAt,
	})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func jsonEqual(left, right []byte) bool {
	var l, r any
	return json.Unmarshal(left, &l) == nil && json.Unmarshal(right, &r) == nil && reflect.DeepEqual(l, r)
}

func observeNativeCheckAndStatusAgain(t *testing.T, db *sql.DB, g githubFixture) bool {
	t.Helper()
	completed := "success"
	run := model.GitHubCheckRun{
		RepositoryID: g.repository, HeadSHA: commitA, ID: 303,
		AppID: 202, Name: "unit", Status: model.CheckRunCompleted, Conclusion: &completed,
		DetailsURL: "https://checks.example/new-details",
	}
	status := model.GitHubCommitStatus{
		RepositoryID: g.repository, SHA: commitA, ID: 51,
		Context: "ci/unit", State: model.CommitStatusSuccess,
		TargetURL: "https://statuses.example/new-details",
	}
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	checkChanged, err := observeGitHubCheckRun(t.Context(), tx, g.app, g.installation, run, time.Now().UTC())
	if err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	statusChanged, err := observeGitHubCommitStatus(t.Context(), tx, g.app, g.installation, status, time.Now().UTC())
	if err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return checkChanged || statusChanged
}

func queryInt64(t *testing.T, db *sql.DB, query string, args ...any) int64 {
	t.Helper()
	var value int64
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}
