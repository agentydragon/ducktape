package github

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
)

var _ worker.Provider = (*Provider)(nil)

// Provider is GitHub as a subscription source: it authorizes a source when a subscription is created, and reads a
// due subscription's deliveries into its inbox.
//
// Every call to GitHub is made with no lock held. What a call learns is recorded on the shared row it refreshed,
// under a lease that an invalidating event or a takeover cancels, so that however many subscriptions need an
// answer, GitHub is asked once.
type Provider struct {
	client *client.Client
	store  *store.Store
	state  *store.GitHubState
	appID  int64
}

func NewProvider(client *client.Client, st *store.Store, settings settings.Settings) *Provider {
	freshness := time.Duration(settings.FreshnessSeconds) * time.Second
	return &Provider{
		client: client, store: st, state: store.NewGitHubState(st, freshness), appID: settings.AppID,
	}
}

// Reconcile reads the deliveries a due GitHub subscription is owed into the claimed inbox, one page at a time. It
// refreshes the access the subscription was authorized under, and the subject, first; if GitHub cannot be asked
// now, the source is deferred, and the cause shows on the shared row that failed rather than on the subscription.
func (p *Provider) Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	source, ok := due.Source.(model.GitHubSource)
	if !ok || due.GitHubAccess == nil {
		return fmt.Errorf("subscription %s is not a bound GitHub subscription", due.ID)
	}
	if due.GitHubAccess.AppID != p.appID {
		return &worker.ProviderError{Kind: store.FailureSourceChanged, Message: "GitHub App changed; recreate the subscription"}
	}
	err := p.reconcile(ctx, claim, due, source)
	var deferred *store.RefreshDeferredError
	if errors.As(err, &deferred) {
		return &worker.ProviderDeferred{Until: deferred.Until}
	}
	return err
}

func (p *Provider) reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription, source model.GitHubSource) error {
	binding := *due.GitHubAccess
	base, err := p.refreshAccess(ctx, binding)
	if err != nil {
		return err
	}
	subject, err := store.NewGitHubSubjectKey(binding.RepositoryID, source.Subject)
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
		return &store.RefreshDeferredError{Until: time.Now()}
	}
	repairAt := state.RepairAt

	// A commit of the subject in another repository is owed only under that repository's current grant.
	grants, err := p.state.RevisionGrants(ctx, binding.AppID, subject)
	if err != nil {
		return err
	}
	fences := []store.GitHubAccessFence{base}
	for _, grant := range grants {
		fence, err := p.refreshAccess(ctx, grant)
		var deferred *store.RefreshDeferredError
		if errors.As(err, &deferred) {
			// The page is made again once the grant may be refreshed.
			repairAt = earlier(repairAt, deferred.Until)
			continue
		}
		if err != nil {
			return err
		}
		fences = append(fences, fence)
	}
	for _, fence := range fences {
		validUntil, err := p.state.ValidUntil(ctx, fence.Key)
		if err != nil {
			return err
		}
		repairAt = earlier(repairAt, validUntil)
	}
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
		key := store.GitHubCommitRefreshKey{
			AppID: p.appID, InstallationID: base.Key.InstallationID, Subject: subject, Event: selection.event,
		}
		resourceRepair, err := p.refreshCommitResource(ctx, key, fences)
		if err != nil {
			return err
		}
		repairAt = earlier(repairAt, resourceRepair)
	}

	if reviewsSelected(source) {
		reviewRepair, err := p.refreshReviews(ctx, subject, base)
		if err != nil {
			return err
		}
		repairAt = earlier(repairAt, reviewRepair)
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
		Fences: fences, Subject: subject, SubjectGeneration: state.Generation, RepairAt: repairAt,
	})
}

func earlier(a, b time.Time) time.Time {
	if b.Before(a) {
		return b
	}
	return a
}
