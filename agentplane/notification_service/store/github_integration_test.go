package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/lease"
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
	destination                   model.DestinationRef
	inbox                         uuid.UUID
	state                         *GitHubState
}

func newGitHubFixture(t *testing.T) githubFixture {
	t.Helper()
	return newGitHubFixtureWithStoreFixture(t, newFixture(t, defaultQuotas()))
}

func newIsolatedGitHubFixture(t *testing.T) githubFixture {
	t.Helper()
	return newGitHubFixtureWithStoreFixture(t, newIsolatedFixture(t, defaultQuotas()))
}

func newGitHubFixtureWithStoreFixture(t *testing.T, f fixture) githubFixture {
	t.Helper()
	base := 1<<41 + rand.Int64N(1<<40)
	ref := model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: testSandboxUID}
	return githubFixture{
		fixture: f, app: base, installation: base + 1, repository: base + 2, destination: ref,
		inbox: f.insertInbox(t, model.DestinationKey(ref, "test-session"), ref, "test-session"),
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

func (g githubFixture) source(subject model.GitHubSubject, events ...model.EventFilter) model.GitHubSource {
	return model.GitHubSource{Repository: testRepositoryName, Subject: subject, Events: events}
}

// A deliveryOption changes the delivery a test makes. Options apply in order, so one that refers to the
// delivery's repository comes after the option that sets it.
type deliveryOption func(*GitHubIngest)

func withAction(value string) deliveryOption { return func(i *GitHubIngest) { i.Action = &value } }

func inInstallation(id int64) deliveryOption { return func(i *GitHubIngest) { i.InstallationID = id } }

func inRepository(id int64, name string) deliveryOption {
	return func(i *GitHubIngest) { i.Repository = &model.GitHubRepository{ID: id, Name: name} }
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
		i.Revisions = append(i.Revisions, model.GitHubHeadRevision{Repository: *i.Repository, SHA: sha})
	}
}

// inFork also puts the delivery's commit in another repository, as a pull request's head is.
func inFork(id int64, name string) deliveryOption {
	return func(i *GitHubIngest) {
		i.Revisions = append(i.Revisions, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: id, Name: name}, SHA: *i.HeadSHA})
	}
}

// delivery is a delivery of the event to the fixture's repository.
func (g githubFixture) delivery(event string, options ...deliveryOption) GitHubIngest {
	id := uuid.New()
	digest := sha256.Sum256(id[:])
	ingest := GitHubIngest{
		AppID: g.app, DeliveryID: id, InstallationID: g.installation,
		Repository: &model.GitHubRepository{ID: g.repository, Name: testRepositoryName},
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
	Source      model.GitHubSource
	Subject     GitHubSubjectKey
}

// follow subscribes the fixture's inbox to the source, under the fixture's grant.
func (g githubFixture) follow(t *testing.T, source model.GitHubSource, key string) githubSubscription {
	t.Helper()
	return g.followIn(t, g.destination, source, key)
}

// followIn subscribes the inbox of another destination of the fixture's owner to the source.
func (g githubFixture) followIn(t *testing.T, destination model.DestinationRef, source model.GitHubSource, key string) githubSubscription {
	t.Helper()
	subject, err := NewGitHubSubjectKey(g.repository, source.Subject)
	if err != nil {
		t.Fatal(err)
	}
	access := g.access()
	subscription, err := g.Subscribe(t.Context(), g.owner, model.Subscribe{
		DestinationRef: destination, SessionID: "test-session", IdempotencyKey: key, Source: source, LifetimeDays: 7,
	}, &access)
	if err != nil {
		t.Fatal(err)
	}
	return githubSubscription{ID: subscription.ID, InboxID: subscription.InboxID, Source: source, Subject: subject}
}

// claimInbox makes the inbox claimed by a worker, as a claim of the inbox does.
func (g githubFixture) claimInbox(t *testing.T, inboxID uuid.UUID) Claim {
	t.Helper()
	holder := uuid.New()
	g.exec(t, `UPDATE inbox SET claim = $2, claim_until = now() + interval '1 hour' WHERE id = $1`, inboxID, holder)
	inbox, err := scanInbox(g.db.QueryRowContext(t.Context(), `SELECT `+inboxColumns+` FROM inbox WHERE id = $1`, inboxID))
	if err != nil {
		t.Fatal(err)
	}
	return Claim{Inbox: inbox, held: lease.Lease{Key: []any{inboxID}, Holder: holder}}
}

// validate refreshes the grant as a successful call to GitHub would, unless it is fresh, and returns its fence.
func (g githubFixture) validate(t *testing.T, key GitHubAccessKey) GitHubAccessFence {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if lease != nil {
		if err := completeEmptyRefresh(t.Context(), g.state, *lease); err != nil {
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
		if err := g.state.CompleteSubject(t.Context(), *lease, []GitHubAccessFence{base}, GitHubSubjectObservation{}); err != nil {
			t.Fatal(err)
		}
	}
}

// addRevisions records commits the subject is known at, as its refresh does.
func (g githubFixture) addRevisions(t *testing.T, subject GitHubSubjectKey, revisions ...model.GitHubHeadRevision) {
	t.Helper()
	err := g.transaction(t.Context(), func(tx *sql.Tx) error { return addRevisions(t.Context(), tx, subject, revisions) })
	if err != nil {
		t.Fatal(err)
	}
}

// forkName is the name of the repository a pull request's head is in.
const forkName = "test-fork-owner/test-repo"

// fork is a repository other than the fixture's, which an installation can be granted access to.
func (g githubFixture) fork(offset int64) model.GitHubRepository {
	return model.GitHubRepository{ID: g.repository + offset, Name: forkName}
}

// grant gives the installation access to the repository, as a pull request's fork has, and returns it.
func (g githubFixture) grant(t *testing.T, installation int64, repository model.GitHubRepository) GitHubAccessKey {
	t.Helper()
	key := GitHubAccessKey{AppID: g.app, InstallationID: installation, RepositoryID: repository.ID}
	if err := g.state.Grant(t.Context(), key, repository.Name); err != nil {
		t.Fatal(err)
	}
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
	subject, err := g.state.Subject(ctx, sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	repairAt := subject.RepairAt
	fences := []GitHubAccessFence{base}
	grants, err := g.state.RevisionGrants(ctx, g.app, sub.Subject)
	if err != nil {
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
			if err := completeEmptyRefresh(ctx, g.state, *lease); err != nil {
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
		validUntil, err := g.state.ValidUntil(ctx, fence.Key)
		if err != nil {
			t.Fatal(err)
		}
		repairAt = minTime(repairAt, validUntil)
	}
	matched, err := g.GitHubDeliveries(ctx, GitHubMatch{
		SubscriptionID: sub.ID, InboxID: sub.InboxID, AppID: g.app, Subject: sub.Subject,
		Filters: sub.Source.Filters(), ObserveCheckRuns: sub.Source.CheckRunObservationsSelected(),
		ObserveCommitStatuses: sub.Source.CommitStatusObservationsSelected(), Fences: fences,
	})
	if err != nil {
		t.Fatal(err)
	}
	return githubStep{GitHubRecording{
		SubscriptionID: sub.ID, Version: version, Generation: generation, Matched: matched,
		More: len(matched) == GitHubDeliveryPageSize, Fences: fences, Subject: sub.Subject,
		SubjectGeneration: subject.Generation, RepairAt: repairAt,
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

// completeEmptyRefresh settles fixtures that intentionally observe no new subject facts or repository name.
func completeEmptyRefresh(ctx context.Context, state *GitHubState, lease GitHubRefreshLease) error {
	switch lease.Key.(type) {
	case GitHubAccessKey:
		return state.CompleteAccess(ctx, lease, nil)
	case GitHubSubjectKey:
		return state.CompleteSubject(ctx, lease, nil, GitHubSubjectObservation{})
	default:
		return errors.New("fixture does not support this refresh kind")
	}
}
