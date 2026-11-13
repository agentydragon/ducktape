package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

type storedDelivery struct {
	installation int64
	repository   sql.NullInt64
	event        string
	action       sql.NullString
	headSHA      sql.NullString
	digest       string
	payload      []byte
}

func (g githubFixture) storedDelivery(t *testing.T, app int64, deliveryID uuid.UUID) (storedDelivery, int64) {
	t.Helper()
	var stored storedDelivery
	var position int64
	if err := g.db.QueryRowContext(t.Context(), `
		SELECT position, installation_id, repository_id, event, action, head_sha, encode(digest, 'hex'), payload
		FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`, app, deliveryID).
		Scan(&position, &stored.installation, &stored.repository, &stored.event, &stored.action, &stored.headSHA, &stored.digest, &stored.payload); err != nil {
		t.Fatal(err)
	}
	return stored, position
}

// links are the subjects a delivery is stored against, as "repository/kind/key".
func (g githubFixture) links(t *testing.T, position int64) []string {
	t.Helper()
	rows, err := g.db.QueryContext(t.Context(),
		`SELECT repository_id, kind, subject_key FROM github_delivery_subject WHERE delivery_position = $1`, position)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var links []string
	for rows.Next() {
		var repository int64
		var kind, key string
		if err := rows.Scan(&repository, &kind, &key); err != nil {
			t.Fatal(err)
		}
		links = append(links, fmt.Sprintf("%d/%s/%s", repository, kind, key))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	slices.Sort(links)
	return links
}

func (g githubFixture) deliveries(t *testing.T) int {
	t.Helper()
	return g.count(t, `SELECT count(*) FROM github_delivery WHERE app_id = $1`, g.app)
}

func TestRedeliveryStoresNothingAndReusedIDIsRefused(t *testing.T) {
	g := newGitHubFixture(t)
	first := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7))

	if g.ingest(t, first) {
		t.Error("a redelivery was stored again")
	}
	if got := g.deliveries(t); got != 1 {
		t.Errorf("%d deliveries, want 1", got)
	}
	changed := first
	digest := sha256.Sum256([]byte("other content"))
	changed.Digest = digest[:]
	if _, err := g.IngestGitHub(t.Context(), changed); !errors.Is(err, ErrDeliveryReused) {
		t.Errorf("error = %v, want ErrDeliveryReused", err)
	}
	if got := g.deliveries(t); got != 1 {
		t.Errorf("%d deliveries after a refused one, want 1", got)
	}
	// The same ID is another delivery under another App.
	other := first
	other.AppID++
	if !g.ingest(t, other) {
		t.Error("a delivery of another App was taken for a redelivery")
	}
}

func TestDeliveryIsStoredAgainstTheSubjectsOfItsRepositoryAndNothingElse(t *testing.T) {
	g := newGitHubFixture(t)
	shaless := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7))
	elsewhere := g.repository + 1
	other := g.deliver(t, "issue_comment", withAction("created"), inRepository(elsewhere, "test-fork-owner/test-repo"), onPullRequest(7))

	stored, position := g.storedDelivery(t, g.app, shaless.DeliveryID)
	if stored.headSHA.Valid {
		t.Errorf("head_sha = %q, want none for an event about no commit", stored.headSHA.String)
	}
	if got, want := g.links(t, position), []string{fmt.Sprintf("%d/pull_request/7", g.repository)}; !slices.Equal(got, want) {
		t.Errorf("links = %v, want %v", got, want)
	}
	_, otherPosition := g.storedDelivery(t, g.app, other.DeliveryID)
	if got, want := g.links(t, otherPosition), []string{fmt.Sprintf("%d/pull_request/7", elsewhere)}; !slices.Equal(got, want) {
		t.Errorf("links = %v, want %v", got, want)
	}

	// A delivery cannot be linked to a subject of a repository it is not in.
	_, err := g.db.ExecContext(t.Context(), `
		INSERT INTO github_delivery_subject (delivery_position, repository_id, kind, subject_key) VALUES ($1, $2, 'pull_request', '7')`,
		position, elsewhere)
	var violation *pq.Error
	if !errors.As(err, &violation) || violation.Code != "23503" {
		t.Errorf("error = %v, want a foreign key violation", err)
	}
}

func TestSubjectKeepsEveryCommitItHasBeenSeenAt(t *testing.T) {
	g := newGitHubFixture(t)
	g.deliver(t, "pull_request", withAction("synchronize"), onPullRequest(7), atCommit(commitB))
	// A delayed, older observation does not replace the newer one.
	g.deliver(t, "pull_request", withAction("synchronize"), onPullRequest(7), atCommit(commitA), inFork(g.repository+1, "test-fork-owner/test-repo"))
	g.deliver(t, "check_run", withAction("completed"), onPullRequest(7), atCommit(commitB))

	rows, err := g.db.QueryContext(t.Context(), `
		SELECT head_repository_id, sha FROM github_subject_revision WHERE repository_id = $1 AND kind = 'pull_request' AND subject_key = '7'`,
		g.repository)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var repository int64
		var sha string
		if err := rows.Scan(&repository, &sha); err != nil {
			t.Fatal(err)
		}
		got = append(got, fmt.Sprintf("%d/%s", repository-g.repository, sha))
	}
	slices.Sort(got)
	want := []string{"0/" + commitA, "0/" + commitB, "1/" + commitA}
	if !slices.Equal(got, want) {
		t.Errorf("revisions = %v, want %v", got, want)
	}
	if name := g.repositoryName(t, g.repository+1); name != "test-fork-owner/test-repo" {
		t.Errorf("fork named %q", name)
	}
}

func (g githubFixture) repositoryName(t *testing.T, id int64) string {
	t.Helper()
	var name string
	if err := g.db.QueryRowContext(t.Context(), `SELECT full_name FROM github_repository WHERE repository_id = $1`, id).Scan(&name); err != nil {
		t.Fatal(err)
	}
	return name
}

func TestRepositoryKeepsTheNameItWasFirstSeenUnder(t *testing.T) {
	g := newGitHubFixture(t)
	g.deliver(t, "issues", onIssue(7))
	g.deliver(t, "issues", onIssue(7), inRepository(g.repository, "test-owner/renamed"))
	if name := g.repositoryName(t, g.repository); name != testRepositoryName {
		t.Errorf("repository named %q, want the first, %q: only a refresh of its access renames it", name, testRepositoryName)
	}
}

func TestInstallationEventNamingNoRepositoryIsStoredWithoutOne(t *testing.T) {
	g := newGitHubFixture(t)
	event := g.delivery("installation", withAction("suspend"))
	event.Repository = nil
	if !g.ingest(t, event) {
		t.Fatal("not stored")
	}
	stored, _ := g.storedDelivery(t, g.app, event.DeliveryID)
	if stored.repository.Valid {
		t.Errorf("repository_id = %d, want none", stored.repository.Int64)
	}
	if got := g.count(t, `SELECT count(*) FROM github_installation WHERE app_id = $1 AND installation_id = $2`, g.app, g.installation); got != 1 {
		t.Errorf("%d installation rows, want 1", got)
	}
}

// invalidationState is what an installation event moves on or drops.
type invalidationState struct {
	installation, access, subject int64
	accessValidUntil              *time.Time
	subjectLastSuccess            *time.Time
	claims                        int
}

func (g githubFixture) invalidationState(t *testing.T, subject GitHubSubjectKey) invalidationState {
	t.Helper()
	var state invalidationState
	var validUntil, lastSuccess sql.NullTime
	if err := g.db.QueryRowContext(t.Context(), `
		SELECT (SELECT generation FROM github_installation WHERE app_id = $1 AND installation_id = $2),
			(SELECT generation FROM github_repository_access WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3),
			(SELECT valid_until FROM github_repository_access WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3),
			(SELECT generation FROM github_subject WHERE repository_id = $3 AND kind = $4 AND subject_key = $5),
			(SELECT last_success_at FROM github_subject WHERE repository_id = $3 AND kind = $4 AND subject_key = $5),
			(SELECT count(*) FROM github_repository_access WHERE app_id = $1 AND claim IS NOT NULL) +
			(SELECT count(*) FROM github_subject WHERE repository_id = $3 AND claim IS NOT NULL)`,
		g.app, g.installation, g.repository, subject.Kind, subject.Key).
		Scan(&state.installation, &state.access, &validUntil, &state.subject, &lastSuccess, &state.claims); err != nil {
		t.Fatal(err)
	}
	if validUntil.Valid {
		state.accessValidUntil = utc(validUntil.Time)
	}
	if lastSuccess.Valid {
		state.subjectLastSuccess = utc(lastSuccess.Time)
	}
	return state
}

func TestInstallationEventsInvalidateWhatWasLearnedUnderTheInstallation(t *testing.T) {
	for _, event := range []string{"installation", "installation_repositories"} {
		t.Run(event, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			g.refreshSubject(t, sub.Subject, g.validate(t, g.access()))
			// A refresh in flight holds a lease on each.
			g.exec(t, `UPDATE github_repository_access SET claim = gen_random_uuid(), claim_until = now() + interval '30 seconds' WHERE app_id = $1`, g.app)
			g.exec(t, `UPDATE github_subject SET claim = gen_random_uuid(), claim_until = now() + interval '30 seconds' WHERE repository_id = $1`, g.repository)
			before := g.invalidationState(t, sub.Subject)
			if before.accessValidUntil == nil || before.subjectLastSuccess == nil || before.claims != 2 {
				t.Fatalf("state before = %+v, want a validated grant and a refreshed subject, both leased", before)
			}

			// An event that is not about the installation changes nothing it holds.
			g.deliver(t, "check_run", withAction("completed"), atCommit(commitA))
			requireEqual(t, g.invalidationState(t, sub.Subject), before)

			g.deliver(t, event, withAction("removed"))
			after := g.invalidationState(t, sub.Subject)
			if after.installation != before.installation+1 || after.access != before.access+1 || after.subject != before.subject+1 {
				t.Errorf("generations after = %+v, want each moved on from %+v", after, before)
			}
			if after.accessValidUntil != nil || after.subjectLastSuccess != nil || after.claims != 0 {
				t.Errorf("state after = %+v, want nothing valid, refreshed or leased", after)
			}
			if _, err := g.state.Fence(t.Context(), g.access()); err == nil {
				t.Error("a fence was taken on an invalidated grant")
			}
		})
	}
}

func TestInstallationEventStopsALeaseInFlightFromRecording(t *testing.T) {
	for _, event := range []string{"installation", "installation_repositories"} {
		t.Run(event, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			access, err := g.state.Acquire(t.Context(), g.access())
			if err != nil || access == nil {
				t.Fatalf("lease = %v, %v", access, err)
			}
			subject, err := g.state.Acquire(t.Context(), sub.Subject)
			if err != nil || subject == nil {
				t.Fatalf("lease = %v, %v", subject, err)
			}

			g.deliver(t, event, withAction("removed"))

			for name, lease := range map[string]*GitHubRefreshLease{"access": access, "subject": subject} {
				var deferred *RefreshDeferredError
				if err := completeEmptyRefresh(t.Context(), g.state, *lease); !errors.As(err, &deferred) {
					t.Errorf("recording the %s refresh = %v, want it deferred", name, err)
				}
			}
			if _, err := g.state.Fence(t.Context(), g.access()); err == nil {
				t.Error("the grant is valid though its refresh was refused")
			}
			replacement, err := g.state.Acquire(t.Context(), g.access())
			if err != nil || replacement == nil {
				t.Fatalf("replacement lease = %v, %v", replacement, err)
			}
			if err := completeEmptyRefresh(t.Context(), g.state, *replacement); err != nil {
				t.Fatal(err)
			}
			if again, err := g.state.Acquire(t.Context(), g.access()); err != nil || again != nil {
				t.Errorf("a fresh grant was leased again: %v, %v", again, err)
			}
		})
	}
}

// wakeState is when a subscription and its inbox are next due.
type wakeState struct {
	generation  int64
	nextAttempt time.Time
	inboxNext   time.Time
}

func (g githubFixture) wakeState(t *testing.T, sub githubSubscription) wakeState {
	t.Helper()
	var state wakeState
	if err := g.db.QueryRowContext(t.Context(), `
		SELECT s.generation, s.next_attempt, i.next_attempt FROM subscription s JOIN inbox i ON i.id = s.inbox_id WHERE s.id = $1`, sub.ID).
		Scan(&state.generation, &state.nextAttempt, &state.inboxNext); err != nil {
		t.Fatal(err)
	}
	state.nextAttempt, state.inboxNext = state.nextAttempt.UTC(), state.inboxNext.UTC()
	return state
}

func TestIngestMakesTheAppsActiveSourcesDueAndLeavesOthersAlone(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(model.PullRequestSubject{Number: 7})
	idle := g.follow(t, source, "idle")
	backedOff := g.followIn(t, otherDestination("backed-off"), source, "backed-off")
	cancelled := g.followIn(t, otherDestination("cancelled"), source, "cancelled")
	expired := g.followIn(t, otherDestination("expired"), source, "expired")
	retired := g.followIn(t, otherDestination("retired"), source, "retired")
	otherApp := newGitHubFixture(t)
	foreign := otherApp.follow(t, source, "other-app")

	g.exec(t, `UPDATE subscription SET cancelled = true WHERE id = $1`, cancelled.ID)
	g.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 minute' WHERE id = $1`, expired.ID)
	g.exec(t, `UPDATE inbox SET retired = true WHERE id = $1`, retired.InboxID)
	g.exec(t, `
		UPDATE subscription SET error = 'rate limited', error_kind = 'rate_limited', error_since = now(), error_observed_at = now(),
			next_attempt = now() + interval '1 hour' WHERE id = $1`, backedOff.ID)
	later := time.Now().Add(24 * time.Hour)
	for _, sub := range []githubSubscription{idle, cancelled, expired, retired} {
		g.exec(t, `UPDATE subscription SET next_attempt = $2 WHERE id = $1`, sub.ID, later)
	}
	g.exec(t, `UPDATE subscription SET next_attempt = $2 WHERE id = $1`, foreign.ID, later)
	g.exec(t, `UPDATE inbox SET next_attempt = $1 WHERE id IN ($2, $3, $4, $5, $6, $7)`, later,
		idle.InboxID, backedOff.InboxID, cancelled.InboxID, expired.InboxID, retired.InboxID, foreign.InboxID)
	backoff := g.wakeState(t, backedOff).nextAttempt
	unaffected := map[string]githubSubscription{"cancelled": cancelled, "expired": expired, "retired inbox": retired, "another App's": foreign}
	was := map[string]wakeState{}
	for name, sub := range unaffected {
		was[name] = g.wakeState(t, sub)
	}

	before := now()
	g.deliver(t, "issues", onIssue(8))

	for name, sub := range map[string]githubSubscription{"idle": idle, "backing off": backedOff} {
		state := g.wakeState(t, sub)
		if state.generation != 1 {
			t.Errorf("%s source generation = %d, want 1", name, state.generation)
		}
		if state.inboxNext.Before(before) || state.inboxNext.After(time.Now()) {
			t.Errorf("%s source's inbox next attempt = %v, want the moment of the delivery", name, state.inboxNext)
		}
	}
	if next := g.wakeState(t, idle).nextAttempt; next.Before(before) || next.After(time.Now()) {
		t.Errorf("idle source next attempt = %v, want the moment of the delivery", next)
	}
	if next := g.wakeState(t, backedOff).nextAttempt; !next.Equal(backoff) {
		t.Errorf("a source backing off from a failure was made due at %v, not left at %v", next, backoff)
	}
	for name, sub := range unaffected {
		if current := g.wakeState(t, sub); current != was[name] {
			t.Errorf("%s source changed from %+v to %+v", name, was[name], current)
		}
	}
}

// otherDestination is another destination of the fixture's owner, for a subscription that must not share the
// fixture's inbox.
func otherDestination(name string) model.DestinationRef {
	return model.DestinationRef{Namespace: "test-namespace", Name: name, UID: "test-uid"}
}

func TestIngestWakesWorkersOnlyForADeliveryItStores(t *testing.T) {
	g := newGitHubFixture(t)
	check := observeWakeups(t, g.fixture)
	delivery := g.delivery("issues", onIssue(7))
	check("a new delivery", true, func() { g.ingest(t, delivery) })
	check("a redelivery", false, func() { g.ingest(t, delivery) })
	check("a refused delivery", false, func() {
		changed := delivery
		changed.Digest = []byte("other")
		if _, err := g.IngestGitHub(t.Context(), changed); !errors.Is(err, ErrDeliveryReused) {
			t.Error(err)
		}
	})
}

func TestStoredDeliveriesAreWhatPythonStores(t *testing.T) {
	for _, vector := range loadGolden(t).Deliveries {
		t.Run(vector.Name, func(t *testing.T) {
			g := newGitHubFixture(t)
			ingest := vector.ingest(t)
			ingest.AppID = g.app
			if !g.ingest(t, ingest) {
				t.Fatal("not stored")
			}

			stored, position := g.storedDelivery(t, g.app, ingest.DeliveryID)
			if stored.installation != ingest.InstallationID || stored.event != ingest.Event || stored.digest != vector.Ingest.Digest {
				t.Errorf("stored %+v, want installation %d, event %q, digest %s", stored, ingest.InstallationID, ingest.Event, vector.Ingest.Digest)
			}
			if !sameText(stored.action, ingest.Action) || !sameText(stored.headSHA, ingest.HeadSHA) {
				t.Errorf("action %+v and head_sha %+v, want %v and %v", stored.action, stored.headSHA, ingest.Action, ingest.HeadSHA)
			}
			if stored.repository.Valid != (ingest.Repository != nil) || (ingest.Repository != nil && stored.repository.Int64 != ingest.Repository.ID) {
				t.Errorf("repository_id = %+v, want %v", stored.repository, ingest.Repository)
			}
			// The body is kept as JSON, whatever the formatting it arrived in.
			sameJSON(t, stored.payload, []byte(vector.Raw))
			sameJSON(t, stored.payload, vector.Ingest.Payload)

			var wantLinks []string
			for _, subject := range ingest.Subjects {
				wantLinks = append(wantLinks, fmt.Sprintf("%d/%s/%s", subject.RepositoryID, subject.Kind, subject.Key))
			}
			slices.Sort(wantLinks)
			if got := g.links(t, position); !slices.Equal(got, slices.Compact(wantLinks)) {
				t.Errorf("links = %v, want %v", got, slices.Compact(wantLinks))
			}
			for _, subject := range ingest.Subjects {
				for _, revision := range ingest.Revisions {
					if got := g.count(t, `
						SELECT count(*) FROM github_subject_revision
						WHERE repository_id = $1 AND kind = $2 AND subject_key = $3 AND head_repository_id = $4 AND sha = $5`,
						subject.RepositoryID, subject.Kind, subject.Key, revision.Repository.ID, revision.SHA); got != 1 {
						t.Errorf("%d rows for revision %+v of %+v, want 1", got, revision, subject)
					}
					if name := g.repositoryName(t, revision.Repository.ID); name != revision.Repository.Name {
						t.Errorf("repository %d named %q, want %q", revision.Repository.ID, name, revision.Repository.Name)
					}
				}
			}
		})
	}
}

func sameText(got sql.NullString, want *string) bool {
	return got.Valid == (want != nil) && (want == nil || got.String == *want)
}

// pythonStored writes a delivery as Python's ingest does, to the rows the schema gives it.
func (g githubFixture) pythonStored(t *testing.T, ingest GitHubIngest) {
	t.Helper()
	g.exec(t, `INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`, ingest.AppID, ingest.InstallationID)
	var repository sql.NullInt64
	if ingest.Repository != nil {
		repository = sql.NullInt64{Int64: ingest.Repository.ID, Valid: true}
		g.exec(t, `INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`, ingest.Repository.ID, ingest.Repository.Name)
	}
	g.exec(t, `
		INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event, action, head_sha, digest, payload)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)`,
		ingest.AppID, ingest.DeliveryID, ingest.InstallationID, repository, ingest.Event, ingest.Action, ingest.HeadSHA, ingest.Digest, string(ingest.Payload))
}

func TestDeliveriesPythonStoredAreRecognisedAsRedeliveries(t *testing.T) {
	for _, vector := range loadGolden(t).Deliveries {
		t.Run(vector.Name, func(t *testing.T) {
			g := newGitHubFixture(t)
			ingest := vector.ingest(t)
			ingest.AppID = g.app
			g.pythonStored(t, ingest)

			if g.ingest(t, ingest) {
				t.Error("a delivery Python stored was stored again")
			}
			if got := g.deliveries(t); got != 1 {
				t.Errorf("%d deliveries, want the 1 Python stored", got)
			}
			digest, err := hex.DecodeString(vector.Ingest.Digest)
			if err != nil {
				t.Fatal(err)
			}
			digest[0] ^= 1
			changed := ingest
			changed.Digest = digest
			if _, err := g.IngestGitHub(t.Context(), changed); !errors.Is(err, ErrDeliveryReused) {
				t.Errorf("error = %v, want ErrDeliveryReused", err)
			}
		})
	}
}

func TestGitHubRetentionDeletesReceiptsAndLinksButPreservesInboxCopies(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	delivered := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("copied"))
	claim := g.claimInbox(t, g.inbox)
	if err := g.record(t, claim, g.prepare(t, sub)); err != nil {
		t.Fatal(err)
	}
	_, position := g.storedDelivery(t, g.app, delivered.DeliveryID)
	cutoff := time.Now().UTC().Add(-7 * 24 * time.Hour).Truncate(time.Microsecond)
	g.exec(t, `UPDATE github_delivery SET received_at = $2 WHERE position = $1`, position, cutoff.Add(-time.Second))
	missed := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("expired before matching"))
	g.exec(t, `UPDATE github_delivery SET received_at = $2 WHERE delivery_id = $1`, missed.DeliveryID, cutoff.Add(-time.Second))
	boundary := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("boundary"))
	g.exec(t, `UPDATE github_delivery SET received_at = $2 WHERE delivery_id = $1`, boundary.DeliveryID, cutoff)
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("recent"))
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.deliveries(t); got != 2 {
		t.Fatalf("retained %d receipts, want 2", got)
	}
	if got := g.links(t, position); len(got) != 0 {
		t.Fatalf("expired links remain: %v", got)
	}
	g.requireDelivered(t, "copied")
	step := g.prepare(t, sub)
	if len(step.Matched) != 2 {
		t.Fatalf("matched %d receipts, want boundary and recent", len(step.Matched))
	}
	if err := g.record(t, claim, step); err != nil {
		t.Fatal(err)
	}
	g.requireDelivered(t, "copied", "boundary", "recent")
	// Raw IDs/digests expire too. Redelivery is new to ingress, but not to an inbox holding it.
	if !g.ingest(t, delivered) {
		t.Fatal("expired ID was still deduplicated")
	}
	if err := g.record(t, claim, g.prepare(t, sub)); err != nil {
		t.Fatal(err)
	}
	g.requireDelivered(t, "copied", "boundary", "recent")
}

func TestGitHubRetentionBoundsEachDeletionBatch(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7))
	g.exec(t, `INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, event, digest, payload, received_at)
		SELECT app_id, gen_random_uuid(), installation_id, event, digest, payload, now() - interval '8 days'
		FROM github_delivery CROSS JOIN generate_series(1,1001) WHERE delivery_id = $1`, seed.DeliveryID)
	cutoff := time.Now().Add(-7 * 24 * time.Hour)
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.deliveries(t); got != 2 {
		t.Fatalf("retained %d receipts, want recent seed and one expired receipt", got)
	}
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.deliveries(t); got != 1 {
		t.Fatalf("retained %d receipts, want recent seed", got)
	}
}
