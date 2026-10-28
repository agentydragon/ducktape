package store

import (
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// requireConstraint requires err to be the database refusing a row for the named constraint, or for any constraint
// of the code.
func requireConstraint(t *testing.T, err error, code pq.ErrorCode, constraint string) {
	t.Helper()
	var refused *pq.Error
	if !errors.As(err, &refused) || refused.Code != code || (constraint != "" && refused.Constraint != constraint) {
		t.Fatalf("error = %v, want %s %q", err, code.Name(), constraint)
	}
}

func (g githubFixture) creation(source model.GitHubSource, key string) model.Subscribe {
	return model.Subscribe{DestinationRef: g.destination, SessionID: "test-session", IdempotencyKey: key, Source: source, LifetimeDays: 7}
}

func TestAGitHubSubscriptionIsBoundToTheAccessItWasAuthorizedUnder(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	github := body("github")
	github.Source = model.GitHubSource{Repository: "owner/repository", Subject: model.PullRequestSubject{Number: 1}}
	access := GitHubAccessKey{AppID: 1, InstallationID: 2, RepositoryID: 3}
	invalidAccess := []GitHubAccessKey{{InstallationID: 2, RepositoryID: 3}, {AppID: 1, RepositoryID: 3}, {AppID: 1, InstallationID: 2}}
	for _, bad := range invalidAccess {
		if _, err := f.Subscribe(t.Context(), f.owner, github, &bad); !errors.Is(err, model.ErrInvalid) {
			t.Errorf("access %+v: error = %v, want model.ErrInvalid", bad, err)
		}
	}
	if _, err := f.Subscribe(t.Context(), f.owner, github, nil); err == nil {
		t.Error("a GitHub subscription was created without the access it was authorized under")
	}
	if _, err := f.Subscribe(t.Context(), f.owner, body("actions"), &access); err == nil {
		t.Error("an Actions subscription was created bound to GitHub access")
	}
	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != 0 {
		t.Fatalf("inboxes = %+v, want none", inboxes)
	}
}

func TestSubscriptionsShareTheRowsOfTheirGrantAndSubject(t *testing.T) {
	g := newGitHubFixture(t)
	first := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "first")
	second := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "overlap")
	other := g.follow(t, g.source(model.PullRequestSubject{Number: 8}), "another-pr")
	if first.ID == second.ID {
		t.Fatal("two keys named one subscription")
	}

	for name, row := range map[string]struct {
		query string
		args  []any
	}{
		"installation": {`SELECT count(*) FROM github_installation WHERE app_id = $1`, []any{g.app}},
		"repository":   {`SELECT count(*) FROM github_repository WHERE repository_id = $1`, []any{g.repository}},
		"grant": {
			`SELECT count(*) FROM github_repository_access WHERE app_id = $1 AND repository_id = $2`,
			[]any{g.app, g.repository},
		},
	} {
		if got := g.count(t, row.query, row.args...); got != 1 {
			t.Errorf("%d %s rows, want 1", got, name)
		}
	}
	if got := g.count(t, `SELECT count(*) FROM github_subject WHERE repository_id = $1`, g.repository); got != 2 {
		t.Errorf("%d subject rows, want 2", got)
	}
	if got := g.count(t, `SELECT count(*) FROM subscription WHERE github_app_id = $1`, g.app); got != 3 {
		t.Errorf("%d subscriptions, want 3", got)
	}

	var installation, repository int64
	var kind, key string
	if err := g.db.QueryRowContext(t.Context(), `
		SELECT github_installation_id, github_repository_id, github_subject_kind, github_subject_key
		FROM subscription WHERE id = $1`, other.ID).Scan(&installation, &repository, &kind, &key); err != nil {
		t.Fatal(err)
	}
	requireEqual(t, []any{installation, repository, kind, key}, []any{g.installation, g.repository, "pull_request", "8"})

	// A subscription cannot claim a subject or a grant that does not exist.
	_, err := g.db.ExecContext(t.Context(), `UPDATE subscription SET github_subject_key = '999' WHERE id = $1`, first.ID)
	requireConstraint(t, err, "23503", "subscription_github_subject")
	_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET github_installation_id = 999 WHERE id = $1`, first.ID)
	requireConstraint(t, err, "23503", "subscription_github_access")
}

func TestAGitHubSubscriptionStartsAtTheNewestDelivery(t *testing.T) {
	g := newGitHubFixture(t)
	g.deliver(t, "issues", onIssue(7))
	latest := g.deliver(t, "issues", onIssue(7))
	_, position := g.storedDelivery(t, g.app, latest.DeliveryID)
	subscription := g.follow(t, g.source(model.IssueSubject{Number: 7}), "follow")

	var start int64
	if err := g.db.QueryRowContext(t.Context(), `SELECT github_start_position FROM subscription WHERE id = $1`, subscription.ID).Scan(&start); err != nil {
		t.Fatal(err)
	}
	if start != position {
		t.Fatalf("start position = %d, want that of the newest delivery, %d", start, position)
	}
}

func TestGitHubSubscriptionStateIsSourceSpecific(t *testing.T) {
	g := newGitHubFixture(t)
	subscription := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	for _, column := range []string{"actions_after_sequence = 0", "github_start_position = NULL", "github_app_id = NULL"} {
		_, err := g.db.ExecContext(t.Context(), `UPDATE subscription SET `+column+` WHERE id = $1`, subscription.ID)
		requireConstraint(t, err, "23514", "subscription_source_state")
	}
}

func TestFilterOrderAndDuplicatesDoNotChangeTheSubscriptionIdentity(t *testing.T) {
	g := newGitHubFixture(t)
	created := g.creation(g.source(
		model.PullRequestSubject{Number: 7},
		model.EventFilter{Event: model.EventIssueComment, Actions: []string{"created", "edited"}},
		model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed"}},
	), "follow")
	original, err := g.Subscribe(t.Context(), g.owner, created, ptr(g.access()))
	if err != nil {
		t.Fatal(err)
	}

	reordered := created
	reordered.Source = g.source(
		model.PullRequestSubject{Number: 7},
		model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed", "completed"}},
		model.EventFilter{Event: model.EventIssueComment, Actions: []string{"edited", "created", "edited"}},
		model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed"}},
	)
	replayed, err := g.Subscribe(t.Context(), g.owner, reordered, ptr(g.access()))
	if err != nil {
		t.Fatal(err)
	}
	if replayed.ID != original.ID {
		t.Fatalf("a replay with the filters reordered and repeated made subscription %s, not %s", replayed.ID, original.ID)
	}

	// Stored creations are read with the same set semantics, whatever order they were written in.
	g.exec(t, `
		UPDATE subscription SET creation = jsonb_set(creation, '{source,events}',
			'[{"event": "issue_comment", "actions": ["edited", "created"]}, {"event": "check_run", "actions": ["completed"]}]')
		WHERE id = $1`, original.ID)
	if again, err := g.Subscribe(t.Context(), g.owner, created, ptr(g.access())); err != nil || again.ID != original.ID {
		t.Fatalf("a replay against a creation stored in another order = %+v, %v; want subscription %s", again, err, original.ID)
	}

	changed := created
	changed.Source = g.source(
		model.PullRequestSubject{Number: 7},
		model.EventFilter{Event: model.EventIssueComment, Actions: []string{"deleted"}},
		model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed"}},
	)
	if _, err := g.Subscribe(t.Context(), g.owner, changed, ptr(g.access())); !errors.Is(err, ErrIdempotencyKeyReused) {
		t.Fatalf("error = %v, want ErrIdempotencyKeyReused", err)
	}
}

func TestADueSubscriptionCarriesTheGitHubAccessItWasAuthorizedUnder(t *testing.T) {
	g := newGitHubFixture(t)
	subscription := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	claim := g.claimInbox(t, g.inbox)

	due, err := g.Source(t.Context(), claim)
	if err != nil || due == nil {
		t.Fatalf("model.Source = %+v, %v; want the subscription", due, err)
	}
	requireEqual(t, due.ID, subscription.ID)
	requireEqual(t, due.GitHubAccess, ptr(g.access()))
	requireEqual(t, due.Generation, int64(0))
	if due.ActionsAfterSequence != nil {
		t.Errorf("a GitHub subscription follows Action request events after %d", *due.ActionsAfterSequence)
	}

	g.deliver(t, "issues", onIssue(8))
	if due, err = g.Source(t.Context(), claim); err != nil || due == nil || due.Generation != 1 {
		t.Fatalf("model.Source after ingress = %+v, %v; want generation 1", due, err)
	}

	actions := g.subscribe(t, body("actions"))
	requireEqual(t, actions.InboxID != uuid.Nil, true)
	g.exec(t, `UPDATE subscription SET cancelled = true WHERE id = $1`, subscription.ID)
	actionsClaim := g.claimInbox(t, actions.InboxID)
	if due, err = g.Source(t.Context(), actionsClaim); err != nil || due == nil || due.GitHubAccess != nil || due.ActionsAfterSequence == nil {
		t.Fatalf("model.Source of an Actions subscription = %+v, %v", due, err)
	}
}
