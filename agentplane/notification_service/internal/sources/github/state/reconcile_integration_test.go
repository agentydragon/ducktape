package state

import (
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func TestTheSharedStateAReconcileReadsIsReadAsItWasWritten(t *testing.T) {
	g := newFixture(t)
	ctx := t.Context()
	subject := g.subject(t, 7)

	name, err := g.state.RepositoryName(ctx, g.repository)
	if err != nil || name != testRepositoryName {
		t.Fatalf("RepositoryName = %q, %v", name, err)
	}
	state, err := g.state.Subject(ctx, subject)
	if err != nil || state != (SubjectState{}) {
		t.Fatalf("Subject = %+v, %v, want an unrefreshed subject", state, err)
	}

	var deferred *RefreshDeferredError
	if _, err := g.state.ValidUntil(ctx, g.access()); !errors.As(err, &deferred) {
		t.Fatalf("ValidUntil of an access never validated: error = %v, want a deferral", err)
	}
	before := time.Now()
	base := g.validate(t, g.access())
	validUntil, err := g.state.ValidUntil(ctx, base.Key)
	if err != nil || validUntil.Before(before.Add(testFreshness-time.Second)) || validUntil.After(time.Now().Add(testFreshness)) {
		t.Fatalf("ValidUntil = %v, %v; want one freshness window from the validation", validUntil, err)
	}
	g.refreshSubject(t, subject, base)
	if state, err = g.state.Subject(ctx, subject); err != nil || state.LastSuccessAt == nil || state.LastSuccessAt.Before(before.Add(-time.Second)) {
		t.Fatalf("Subject after a refresh = %+v, %v", state, err)
	}
}

func TestOnlyTheGrantsOnRepositoriesHoldingARevisionOfTheSubjectAreRevisionGrants(t *testing.T) {
	g := newFixture(t)
	ctx := t.Context()
	subject := g.subject(t, 7)
	other := g.subject(t, 8)
	fork := g.fork(1)
	key := g.grant(t, g.installation+1, fork)
	// The grant is a no-op the second time.
	if err := g.state.Grant(ctx, key, fork.Name); err != nil {
		t.Fatal(err)
	}
	// A grant of an App other than the subject's is not shared.
	if err := g.state.Grant(ctx, AccessKey{AppID: g.app + 1, InstallationID: g.installation + 5, RepositoryID: fork.ID}, fork.Name); err != nil {
		t.Fatal(err)
	}

	grants, err := g.state.RevisionGrants(ctx, g.app, subject)
	if err != nil || len(grants) != 0 {
		t.Fatalf("RevisionGrants = %+v, %v, want none", grants, err)
	}
	g.addRevisions(t, subject, model.GitHubHeadRevision{Repository: fork, SHA: commitA})
	// The subject's own repository holds its revisions too, and its grant is the base access, not a fork's.
	g.addRevisions(t, subject, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA})

	grants, err = g.state.RevisionGrants(ctx, g.app, subject)
	if err != nil || !slices.Equal(grants, []AccessKey{key}) {
		t.Fatalf("RevisionGrants = %+v, %v, want %+v", grants, err, []AccessKey{key})
	}
	grants, err = g.state.RevisionGrants(ctx, g.app, other)
	if err != nil || len(grants) != 0 {
		t.Fatalf("RevisionGrants = %+v, %v, want none", grants, err)
	}
}
