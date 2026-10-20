package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// These helpers stand in for the parts of the GitHub source that call GitHub and subscribe: they write the shared
// rows a subscription binds to as Python's subscribe does, and take the steps of a reconcile that touch only the
// store.

const (
	testFreshness      = 10 * time.Minute
	testRepositoryName = "test-owner/test-repo"
)

var (
	commitA = strings.Repeat("a", 40)
	commitB = strings.Repeat("b", 40)
	commitC = strings.Repeat("c", 40)
)

// githubFixture is an App with one installation and one repository that no other test uses, one inbox that its
// subscriptions deliver to, and the shared GitHub state.
type githubFixture struct {
	fixture
	app, installation, repository int64
	inbox                         uuid.UUID
	state                         *GitHubState
}

func newGitHubFixture(t *testing.T) githubFixture {
	t.Helper()
	f := newFixture(t, defaultQuotas())
	base := 1<<41 + rand.Int64N(1<<40)
	ref := DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}
	return githubFixture{
		fixture: f, app: base, installation: base + 1, repository: base + 2,
		inbox: f.insertInbox(t, DestinationKey(ref, "test-session"), ref, "test-session"),
		state: NewGitHubState(f.Store, testFreshness),
	}
}

// access is the installation's grant on the fixture's repository.
func (g githubFixture) access() GitHubAccessKey {
	return GitHubAccessKey{AppID: g.app, InstallationID: g.installation, RepositoryID: g.repository}
}

func (g githubFixture) pullRequest(number int64) GitHubSubjectKey {
	return GitHubSubjectKey{RepositoryID: g.repository, Kind: "pull_request", Key: fmt.Sprint(number)}
}

func (g githubFixture) source(subject GitHubSubject, events ...EventFilter) GitHubSource {
	return GitHubSource{Repository: testRepositoryName, Subject: subject, Events: events}
}

// A deliveryOption changes the delivery a test makes. Options apply in order, so one that refers to the
// delivery's repository comes after the option that sets it.
type deliveryOption func(*GitHubIngest)

func action(value string) deliveryOption { return func(i *GitHubIngest) { i.Action = &value } }

func inInstallation(id int64) deliveryOption { return func(i *GitHubIngest) { i.InstallationID = id } }

func inRepository(id int64, name string) deliveryOption {
	return func(i *GitHubIngest) { i.Repository = &GitHubRepository{ID: id, Name: name} }
}

// labelled sets a payload that tells the delivery apart in an inbox.
func labelled(label string) deliveryOption {
	return func(i *GitHubIngest) { i.Payload = json.RawMessage(fmt.Sprintf(`{"label": %q}`, label)) }
}

func aboutSubject(kind, key string) deliveryOption {
	return func(i *GitHubIngest) {
		i.Subjects = append(i.Subjects, GitHubSubjectKey{RepositoryID: i.Repository.ID, Kind: kind, Key: key})
	}
}

func onPullRequest(number int64) deliveryOption {
	return aboutSubject("pull_request", fmt.Sprint(number))
}
func onIssue(number int64) deliveryOption { return aboutSubject("issue", fmt.Sprint(number)) }
func onBranch(name string) deliveryOption { return aboutSubject("branch", name) }

// atCommit makes the delivery about a commit, seen in the delivery's repository.
func atCommit(sha string) deliveryOption {
	return func(i *GitHubIngest) {
		i.HeadSHA = &sha
		i.Revisions = append(i.Revisions, GitHubHeadRevision{Repository: *i.Repository, SHA: sha})
	}
}

// inFork also puts the delivery's commit in another repository, as a pull request's head is.
func inFork(id int64, name string) deliveryOption {
	return func(i *GitHubIngest) {
		i.Revisions = append(i.Revisions, GitHubHeadRevision{Repository: GitHubRepository{ID: id, Name: name}, SHA: *i.HeadSHA})
	}
}

// delivery is a delivery of the event to the fixture's repository.
func (g githubFixture) delivery(event string, options ...deliveryOption) GitHubIngest {
	id := uuid.New()
	digest := sha256.Sum256(id[:])
	ingest := GitHubIngest{
		AppID: g.app, DeliveryID: id, InstallationID: g.installation,
		Repository: &GitHubRepository{ID: g.repository, Name: testRepositoryName},
		Event:      event, Digest: digest[:], Payload: json.RawMessage(fmt.Sprintf(`{"delivery": %q}`, id)),
	}
	for _, option := range options {
		option(&ingest)
	}
	return ingest
}

func (g githubFixture) ingest(t *testing.T, ingest GitHubIngest) bool {
	t.Helper()
	created, err := g.IngestGitHub(t.Context(), ingest)
	if err != nil {
		t.Fatal(err)
	}
	return created
}

// deliver ingests a new delivery of the event to the fixture's repository.
func (g githubFixture) deliver(t *testing.T, event string, options ...deliveryOption) GitHubIngest {
	t.Helper()
	ingest := g.delivery(event, options...)
	if !g.ingest(t, ingest) {
		t.Fatalf("delivery %s of %s was stored before", ingest.DeliveryID, event)
	}
	return ingest
}

type githubSubscription struct {
	ID, InboxID uuid.UUID
	Source      GitHubSource
	Subject     GitHubSubjectKey
}

// follow subscribes the fixture's inbox to the source, as Python's subscribe does: under the GitHub lock, at the
// newest delivery, over rows it creates for the grant and the subject if they are not there.
func (g githubFixture) follow(t *testing.T, source GitHubSource, key string) githubSubscription {
	t.Helper()
	return g.followIn(t, g.inbox, source, key)
}

func (g githubFixture) followIn(t *testing.T, inboxID uuid.UUID, source GitHubSource, key string) githubSubscription {
	t.Helper()
	subject, err := NewGitHubSubjectKey(g.repository, source.Subject)
	if err != nil {
		t.Fatal(err)
	}
	creation, err := json.Marshal(Subscribe{
		DestinationRef: DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: key, Source: source, LifetimeDays: 7,
	})
	if err != nil {
		t.Fatal(err)
	}
	subscription := githubSubscription{ID: uuid.New(), InboxID: inboxID, Source: source, Subject: subject}
	err = g.transaction(t.Context(), func(tx *sql.Tx) error {
		if err := githubLock(t.Context(), tx); err != nil {
			return err
		}
		var start int64
		if err := tx.QueryRowContext(t.Context(), `SELECT coalesce(max(position), 0) FROM github_delivery`).Scan(&start); err != nil {
			return err
		}
		for _, statement := range []struct {
			query string
			args  []any
		}{
			{
				`INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`,
				[]any{g.app, g.installation},
			},
			{
				`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
				[]any{g.repository, testRepositoryName},
			},
			{`INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES ($1, $2, $3, 0)
				ON CONFLICT DO NOTHING`, []any{g.app, g.installation, g.repository}},
			{
				`INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0) ON CONFLICT DO NOTHING`,
				[]any{subject.RepositoryID, subject.Kind, subject.Key},
			},
			{
				`INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, generation, github_start_position,
					github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key,
					cancelled, expires_at, next_attempt)
				VALUES ($1, $2, $3, $4::jsonb, 1, 0, $5, $6, $7, $8, $9, $10, false, now() + interval '1 day', now())`,
				[]any{subscription.ID, inboxID, key, string(creation), start, g.app, g.installation, g.repository, subject.Kind, subject.Key},
			},
			{`UPDATE inbox SET next_attempt = now() WHERE id = $1`, []any{inboxID}},
		} {
			if _, err := tx.ExecContext(t.Context(), statement.query, statement.args...); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

// claimInbox makes the inbox claimed by a worker, as a claim of the inbox does.
func (g githubFixture) claimInbox(t *testing.T, inboxID uuid.UUID) Claim {
	t.Helper()
	lease := uuid.New()
	g.exec(t, `UPDATE inbox SET claim = $2, claim_until = now() + interval '1 hour' WHERE id = $1`, inboxID, lease)
	inbox, err := scanInbox(g.db.QueryRowContext(t.Context(), `SELECT `+inboxColumns+` FROM inbox WHERE id = $1`, inboxID))
	if err != nil {
		t.Fatal(err)
	}
	return Claim{Inbox: inbox, lease: lease}
}

// validate refreshes the grant as a successful call to GitHub would, unless it is fresh, and returns its fence.
func (g githubFixture) validate(t *testing.T, key GitHubAccessKey) GitHubAccessFence {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if lease != nil {
		if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{}); err != nil {
			t.Fatal(err)
		}
	}
	fence, err := g.state.Fence(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	return fence
}

// refreshSubject refreshes the subject as a successful call to GitHub would, unless it is fresh.
func (g githubFixture) refreshSubject(t *testing.T, subject GitHubSubjectKey, base GitHubAccessFence) {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), subject)
	if err != nil {
		t.Fatal(err)
	}
	if lease != nil {
		if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{Fences: []GitHubAccessFence{base}}); err != nil {
			t.Fatal(err)
		}
	}
}

// addRevisions records commits the subject is known at, as its refresh does.
func (g githubFixture) addRevisions(t *testing.T, subject GitHubSubjectKey, revisions ...GitHubHeadRevision) {
	t.Helper()
	err := g.transaction(t.Context(), func(tx *sql.Tx) error { return addRevisions(t.Context(), tx, subject, revisions) })
	if err != nil {
		t.Fatal(err)
	}
}

// forkName is the name of the repository a pull request's head is in.
const forkName = "test-fork-owner/test-repo"

// fork is a repository other than the fixture's, which an installation can be granted access to.
func (g githubFixture) fork(offset int64) GitHubRepository {
	return GitHubRepository{ID: g.repository + offset, Name: forkName}
}

// grant gives the installation access to the repository, as a pull request's fork has, and returns it.
func (g githubFixture) grant(t *testing.T, installation int64, repository GitHubRepository) GitHubAccessKey {
	t.Helper()
	key := GitHubAccessKey{AppID: g.app, InstallationID: installation, RepositoryID: repository.ID}
	g.exec(t, `INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`, g.app, installation)
	g.exec(t, `INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`, repository.ID, repository.Name)
	g.exec(t, `INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES ($1, $2, $3, 0)`,
		key.AppID, key.InstallationID, key.RepositoryID)
	return key
}

// A githubStep is what a worker has read to record one page for a subscription.
type githubStep struct {
	GitHubRecording
}

// prepare is the first half of a reconcile, without GitHub: it refreshes the grant and the subject, reads the
// subject's state and the fences of the grants that hold a revision of it, and matches a page of deliveries.
func (g githubFixture) prepare(t *testing.T, sub githubSubscription) githubStep {
	t.Helper()
	ctx := t.Context()
	var version, generation int64
	if err := g.db.QueryRowContext(ctx, `SELECT version, generation FROM subscription WHERE id = $1`, sub.ID).Scan(&version, &generation); err != nil {
		t.Fatal(err)
	}
	base := g.validate(t, g.access())
	g.refreshSubject(t, sub.Subject, base)
	subject, err := loadRefresh(ctx, g.db, sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	repairAt := subject.lastSuccessAt.Time.Add(testFreshness)
	fences := []GitHubAccessFence{base}
	rows, err := g.db.QueryContext(ctx, `
		SELECT installation_id, repository_id FROM github_repository_access
		WHERE app_id = $1 AND repository_id != $2 AND repository_id IN (
			SELECT head_repository_id FROM github_subject_revision WHERE repository_id = $2 AND kind = $3 AND subject_key = $4)`,
		g.app, g.repository, sub.Subject.Kind, sub.Subject.Key)
	if err != nil {
		t.Fatal(err)
	}
	var grants []GitHubAccessKey
	for rows.Next() {
		grant := GitHubAccessKey{AppID: g.app}
		if err := rows.Scan(&grant.InstallationID, &grant.RepositoryID); err != nil {
			t.Fatal(err)
		}
		grants = append(grants, grant)
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	for _, grant := range grants {
		if lease, err := g.state.Acquire(ctx, grant); err != nil {
			// A grant that cannot be refreshed now is left out, and the page is made again when it may be.
			var deferred *RefreshDeferredError
			if !errors.As(err, &deferred) {
				t.Fatal(err)
			}
			repairAt = minTime(repairAt, deferred.Until)
			continue
		} else if lease != nil {
			if err := g.state.Succeed(ctx, *lease, GitHubSuccess{}); err != nil {
				t.Fatal(err)
			}
		}
		fence, err := g.state.Fence(ctx, grant)
		if err != nil {
			t.Fatal(err)
		}
		fences = append(fences, fence)
	}
	for _, fence := range fences {
		access, err := loadRefresh(ctx, g.db, fence.Key)
		if err != nil {
			t.Fatal(err)
		}
		repairAt = minTime(repairAt, access.validUntil.Time)
	}
	matched, err := g.GitHubDeliveries(ctx, GitHubMatch{
		SubscriptionID: sub.ID, InboxID: sub.InboxID, AppID: g.app, Subject: sub.Subject,
		Filters: sub.Source.Filters(), Fences: fences,
	})
	if err != nil {
		t.Fatal(err)
	}
	return githubStep{GitHubRecording{
		SubscriptionID: sub.ID, Version: version, Generation: generation, Matched: matched,
		More: len(matched) == GitHubDeliveryPageSize, Fences: fences, Subject: sub.Subject,
		SubjectGeneration: subject.generation, RepairAt: repairAt,
	}}
}

// record is the second half of a reconcile.
func (g githubFixture) record(t *testing.T, claim Claim, step githubStep) error {
	t.Helper()
	return g.RecordGitHub(t.Context(), claim, step.GitHubRecording)
}

// reconcile matches and records a page for the subscription.
func (g githubFixture) reconcile(t *testing.T, claim Claim, sub githubSubscription) error {
	t.Helper()
	return g.record(t, claim, g.prepare(t, sub))
}

func minTime(a, b time.Time) time.Time {
	if b.Before(a) {
		return b
	}
	return a
}

// labels are the labels of the entries' payloads.
func labels(t *testing.T, entries []Entry) []string {
	t.Helper()
	var got []string
	for _, entry := range entries {
		var payload struct {
			Label string `json:"label"`
		}
		if err := json.Unmarshal(entry.Payload, &payload); err != nil {
			t.Fatal(err)
		}
		got = append(got, payload.Label)
	}
	return got
}

// delivered is the labels of what the inbox holds.
func (g githubFixture) delivered(t *testing.T) []string {
	t.Helper()
	return labels(t, g.read(t, g.inbox, 0).Entries)
}

type subscriptionState struct {
	generation  int64
	nextAttempt sql.NullTime
	lastSuccess sql.NullTime
	errorKind   sql.NullString
}

func (g githubFixture) subscriptionState(t *testing.T, id uuid.UUID) subscriptionState {
	t.Helper()
	var state subscriptionState
	if err := g.db.QueryRowContext(t.Context(), `SELECT generation, next_attempt, last_success_at, error_kind FROM subscription WHERE id = $1`, id).
		Scan(&state.generation, &state.nextAttempt, &state.lastSuccess, &state.errorKind); err != nil {
		t.Fatal(err)
	}
	return state
}
