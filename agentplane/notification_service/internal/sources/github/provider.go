package github

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

var _ sources.Provider = (*Provider)(nil)

// Provider is GitHub as a subscription source: it authorizes a source when a subscription is created, and reads a
// due subscription's deliveries into its inbox.
//
// Every call to GitHub is made with no lock held. What a call learns is recorded on the shared row it refreshed,
// under a lease that an invalidating event or a takeover cancels, so that however many subscriptions need an
// answer, GitHub is asked once.
type Provider struct {
	client *client.Client
	store  *store.Store
	state  *githubstate.State
	appID  int64
}

func NewProvider(client *client.Client, st *store.Store, sharedState *githubstate.State, settings settings.Settings) *Provider {
	return &Provider{
		client: client, store: st, state: sharedState, appID: settings.AppID,
	}
}

// Process reads the deliveries a due GitHub subscription is owed into the claimed inbox, one page at a time. It
// refreshes the access the subscription was authorized under, and the subject, first; if GitHub cannot be asked
// now, the source is deferred, and the cause shows on the shared row that failed rather than on the subscription.
func (p *Provider) Process(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	source, ok := due.Source.(model.GitHubSource)
	if !ok || due.GitHubAccess == nil {
		return fmt.Errorf("subscription %s is not a bound GitHub subscription", due.ID)
	}
	if due.GitHubAccess.AppID != p.appID {
		return &sources.ProviderError{Kind: model.FailureSourceChanged, Message: "GitHub App changed; recreate the subscription"}
	}
	err := p.reconcile(ctx, claim, due, source)
	var deferred *githubstate.RefreshDeferredError
	if errors.As(err, &deferred) {
		return &sources.ProviderDeferred{Until: deferred.Until}
	}
	return err
}

func (p *Provider) reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription, source model.GitHubSource) error {
	binding := *due.GitHubAccess
	base, err := p.refreshAccess(ctx, binding)
	if err != nil {
		return err
	}
	subject, err := githubstate.NewSubjectKey(binding.RepositoryID, source.Subject)
	if err != nil {
		return err
	}
	if err := p.refreshSubject(ctx, source, subject, base); err != nil {
		return err
	}
	state, err := p.state.Subject(ctx, subject)
	if err != nil {
		return err
	}
	if state.LastSuccessAt == nil {
		return &githubstate.RefreshDeferredError{Until: time.Now()}
	}
	var retryAt *time.Time

	// A commit of the subject in another repository is owed only under that repository's current grant.
	grants, err := p.state.RevisionGrants(ctx, binding.AppID, subject)
	if err != nil {
		return err
	}
	fences := []githubstate.AccessFence{base}
	for _, grant := range grants {
		fence, err := p.refreshAccess(ctx, grant)
		var deferred *githubstate.RefreshDeferredError
		if errors.As(err, &deferred) {
			// The page is made again once the grant may be refreshed.
			retryAt = earlierRetryAt(retryAt, &deferred.Until)
			continue
		}
		if err != nil {
			return err
		}
		fences = append(fences, fence)
	}
	repairAt, err := p.earliestFenceExpiry(ctx, fences)
	if err != nil {
		return err
	}
	retryAt = earlierRetryAt(retryAt, state.RetryAt)
	for _, selection := range []struct {
		event    model.EventName
		selected bool
	}{
		{model.EventCheckRun, source.CheckRunObservationsSelected()},
		{model.EventStatus, source.CommitStatusObservationsSelected()},
	} {
		if !selection.selected {
			continue
		}
		key := githubstate.CommitRefreshKey{
			AppID: p.appID, InstallationID: base.Key.InstallationID, Subject: subject, Event: selection.event,
		}
		resourceRetry, err := p.refreshCommitResource(ctx, key, fences)
		if err != nil {
			return err
		}
		retryAt = earlierRetryAt(retryAt, resourceRetry)
	}

	if reviewsSelected(source) {
		reviewRetry, err := p.refreshReviews(ctx, subject, base)
		if err != nil {
			return err
		}
		retryAt = earlierRetryAt(retryAt, reviewRetry)
	}

	deliveries, err := p.store.GitHubDeliveries(ctx, store.GitHubMatch{
		SubscriptionID: due.ID, InboxID: claim.ID, AppID: binding.AppID, Subject: subject,
		Filters: source.Filters(), Fences: fences,
		ObserveCheckRuns:      source.CheckRunObservationsSelected(),
		ObserveCommitStatuses: source.CommitStatusObservationsSelected(),
	})
	if err != nil {
		return err
	}
	return p.store.RecordGitHub(ctx, claim, store.GitHubRecording{
		SubscriptionID: due.ID, Version: due.Version, Generation: due.Generation,
		Matched: deliveries, More: len(deliveries) == store.GitHubDeliveryPageSize,
		Fences: fences, Subject: subject, SubjectGeneration: state.Generation, RepairAt: repairAt, RetryAt: retryAt,
	})
}

func (p *Provider) earliestFenceExpiry(ctx context.Context, fences []githubstate.AccessFence) (time.Time, error) {
	var earliest time.Time
	for _, fence := range fences {
		validUntil, err := p.state.ValidUntil(ctx, fence.Key)
		if err != nil {
			return time.Time{}, err
		}
		if earliest.IsZero() || validUntil.Before(earliest) {
			earliest = validUntil
		}
	}
	return earliest, nil
}

func earlierRetryAt(a, b *time.Time) *time.Time {
	if b == nil || (a != nil && !b.Before(*a)) {
		return a
	}
	return b
}
