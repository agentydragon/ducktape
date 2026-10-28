package github

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

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
	client *Client
	store  *store.Store
	state  *store.GitHubState
	appID  int64
}

func NewProvider(client *Client, st *store.Store, settings Settings) *Provider {
	freshness := time.Duration(settings.FreshnessSeconds) * time.Second
	return &Provider{
		client: client, store: st, state: store.NewGitHubState(st, freshness), appID: settings.AppID,
	}
}

// access is the App's access to a repository through the installation that covers it.
func (p *Provider) access(ctx context.Context, repository string) (store.GitHubAccessKey, error) {
	installation, err := p.client.Installation(ctx, repository)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	read, err := p.client.Repository(ctx, repository, installation)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	return store.GitHubAccessKey{AppID: p.appID, InstallationID: installation, RepositoryID: read.ID}, nil
}

// forkAccess is the App's access to the repository a pull request's head is in, or nil while the App is not
// installed there: an installation on the base repository does not extend to a fork, so a fork the App cannot see
// is left out, not an error. A suspended installation is an error.
func (p *Provider) forkAccess(ctx context.Context, fork Repository) (*store.GitHubAccessKey, error) {
	access, err := p.access(ctx, fork.FullName)
	var notInstalled *NotInstalledError
	switch {
	case errors.As(err, &notInstalled):
		slog.WarnContext(ctx, "pull request fork is not covered", "fork", fork.FullName)
		return nil, nil
	case err != nil:
		return nil, err
	case access.RepositoryID != fork.ID:
		return nil, &SourceChangedError{Reason: "GitHub fork repository identity changed"}
	}
	return &access, nil
}

// Authorize checks that the App can read the source, and returns the access it reads the source under: the
// installation that covers the source's repository. It fails with the client's typed errors.
func (p *Provider) Authorize(ctx context.Context, source store.GitHubSource) (store.GitHubAccessKey, error) {
	access, err := p.access(ctx, source.Repository)
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	switch subject := source.Subject.(type) {
	case store.PullRequestSubject:
		pullRequest, err := p.client.PullRequest(ctx, source.Repository, int(subject.Number), access.InstallationID)
		if err != nil {
			return store.GitHubAccessKey{}, err
		}
		if pullRequest.Head.Repository != nil && pullRequest.Head.Repository.ID != access.RepositoryID {
			if _, err := p.forkAccess(ctx, *pullRequest.Head.Repository); err != nil {
				return store.GitHubAccessKey{}, err
			}
		}
	case store.IssueSubject:
		err = p.client.CheckIssue(ctx, source.Repository, int(subject.Number), access.InstallationID)
	case store.BranchSubject:
		_, _, err = p.client.Branch(ctx, source.Repository, subject.Name, access.InstallationID)
	case store.CommitSubject:
		err = p.client.CheckCommit(ctx, source.Repository, subject.SHA, access.InstallationID)
	default:
		err = fmt.Errorf("unsupported GitHub subject %T", subject)
	}
	if err != nil {
		return store.GitHubAccessKey{}, err
	}
	return access, nil
}

// Reconcile reads the deliveries a due GitHub subscription is owed into the claimed inbox, one page at a time. It
// refreshes the access the subscription was authorized under, and the subject, first; if GitHub cannot be asked
// now, the source is deferred, and the cause shows on the shared row that failed rather than on the subscription.
func (p *Provider) Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	source, ok := due.Source.(store.GitHubSource)
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

func (p *Provider) reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription, source store.GitHubSource) error {
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
		event    store.EventName
		selected bool
	}{
		{store.EventCheckRun, source.CheckRunObservationsSelected()},
		{store.EventStatus, source.CommitStatusObservationsSelected()},
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

// Each commit resource has its own deadline and error state. Pending mergeability retries do not accelerate
// either read, and a classified failure in one resource does not stop the other's observations or raw activity.
func (p *Provider) refreshCommitResource(ctx context.Context, key store.GitHubCommitRefreshKey, fences []store.GitHubAccessFence) (time.Time, error) {
	lease, err := p.state.Acquire(ctx, key)
	var deferred *store.RefreshDeferredError
	if errors.As(err, &deferred) {
		return deferred.Until, nil
	}
	if err != nil {
		return time.Time{}, err
	}
	if lease != nil {
		success := store.GitHubSuccess{Fences: fences}
		switch key.Event {
		case store.EventCheckRun:
			success.CheckRuns, err = p.readCheckRuns(ctx, key.Subject, fences)
		case store.EventStatus:
			success.CommitStatuses, err = p.readCommitStatuses(ctx, key.Subject, fences)
		default:
			return time.Time{}, fmt.Errorf("unsupported commit resource %q", key.Event)
		}
		if err != nil {
			err = p.refreshFailed(ctx, *lease, err)
		} else {
			err = p.state.Succeed(ctx, *lease, success)
		}
		if errors.As(err, &deferred) {
			return deferred.Until, nil
		}
		if err != nil {
			return time.Time{}, err
		}
	}
	state, err := p.state.CommitState(ctx, key)
	return state.RepairAt, err
}

type commitTarget struct {
	repository     store.GitHubRepository
	sha            string
	installationID int64
}

func (p *Provider) commitTargets(ctx context.Context, subject store.GitHubSubjectKey, fences []store.GitHubAccessFence) ([]commitTarget, error) {
	revisions, err := p.state.CommitTargets(ctx, subject)
	if err != nil {
		return nil, err
	}
	var targets []commitTarget
	for _, revision := range revisions {
		for _, fence := range fences {
			if fence.Key.RepositoryID == revision.Repository.ID {
				targets = append(targets, commitTarget{repository: revision.Repository, sha: revision.SHA, installationID: fence.Key.InstallationID})
				break
			}
		}
	}
	return targets, nil
}

func (p *Provider) readCheckRuns(ctx context.Context, subject store.GitHubSubjectKey, fences []store.GitHubAccessFence) ([]store.GitHubCheckRun, error) {
	targets, err := p.commitTargets(ctx, subject, fences)
	if err != nil {
		return nil, err
	}
	observations := make([]store.GitHubCheckRun, 0)
	for _, target := range targets {
		runs, err := p.client.CheckRuns(ctx, target.repository.Name, target.sha, target.installationID)
		if err != nil {
			return nil, err
		}
		for i := range runs {
			runs[i].RepositoryID = target.repository.ID
		}
		observations = append(observations, runs...)
	}
	return observations, nil
}

func (p *Provider) readCommitStatuses(ctx context.Context, subject store.GitHubSubjectKey, fences []store.GitHubAccessFence) ([]store.GitHubCommitStatus, error) {
	targets, err := p.commitTargets(ctx, subject, fences)
	if err != nil {
		return nil, err
	}
	observations := make([]store.GitHubCommitStatus, 0)
	for _, target := range targets {
		statuses, err := p.client.CommitStatuses(ctx, target.repository.Name, target.sha, target.installationID)
		if err != nil {
			return nil, err
		}
		for i := range statuses {
			statuses[i].RepositoryID = target.repository.ID
		}
		observations = append(observations, statuses...)
	}
	return observations, nil
}

func earlier(a, b time.Time) time.Time {
	if b.Before(a) {
		return b
	}
	return a
}

// refreshAccess returns the fence of a valid access, asking GitHub first if the access needs validating and no
// other refresh holds it.
func (p *Provider) refreshAccess(ctx context.Context, key store.GitHubAccessKey) (store.GitHubAccessFence, error) {
	lease, err := p.state.Acquire(ctx, key)
	if err != nil {
		return store.GitHubAccessFence{}, err
	}
	if lease != nil {
		name, err := p.readAccess(ctx, key)
		if err != nil {
			return store.GitHubAccessFence{}, p.refreshFailed(ctx, *lease, err)
		}
		if err := p.state.Succeed(ctx, *lease, store.GitHubSuccess{RepositoryName: &name}); err != nil {
			return store.GitHubAccessFence{}, err
		}
	}
	return p.state.Fence(ctx, key)
}

// readAccess returns the repository's current name if GitHub still shows the repository under the installation.
func (p *Provider) readAccess(ctx context.Context, key store.GitHubAccessKey) (string, error) {
	repository, err := p.client.RepositoryByID(ctx, key.RepositoryID, key.InstallationID)
	if err != nil {
		return "", err
	}
	installation, err := p.client.Installation(ctx, repository.FullName)
	if err != nil {
		return "", err
	}
	if repository.ID != key.RepositoryID || installation != key.InstallationID {
		return "", &SourceChangedError{Reason: "GitHub repository/installation identity changed"}
	}
	return repository.FullName, nil
}

// refreshSubject refreshes the subject unless it is fresh or another refresh holds it: the commits it is at, in
// the repositories holding them, and the grant on a pull request's fork when the App is installed there.
func (p *Provider) refreshSubject(ctx context.Context, source store.GitHubSource, subject store.GitHubSubjectKey, base store.GitHubAccessFence) error {
	lease, err := p.state.Acquire(ctx, subject)
	if err != nil || lease == nil {
		return err
	}
	observation, err := p.readSubject(ctx, source, subject, base.Key.InstallationID)
	if err != nil {
		return p.refreshFailed(ctx, *lease, err)
	}
	return p.state.Succeed(ctx, *lease, store.GitHubSuccess{
		Fences: []store.GitHubAccessFence{base}, Revisions: observation.revisions, PullRequest: observation.pullRequest,
	})
}

type subjectObservation struct {
	revisions   []store.GitHubHeadRevision
	pullRequest *store.GitHubPullRequestObservation
}

func (p *Provider) readSubject(ctx context.Context, source store.GitHubSource, subject store.GitHubSubjectKey, installation int64) (subjectObservation, error) {
	name, err := p.state.RepositoryName(ctx, subject.RepositoryID)
	if err != nil {
		return subjectObservation{}, err
	}
	here := store.GitHubRepository{ID: subject.RepositoryID, Name: name}
	switch s := source.Subject.(type) {
	case store.PullRequestSubject:
		pullRequest, err := p.client.PullRequest(ctx, name, int(s.Number), installation)
		if err != nil {
			return subjectObservation{}, err
		}
		revisions := []store.GitHubHeadRevision{{Repository: here, SHA: pullRequest.Head.SHA}}
		// Base-repository CI may build the fork's head, so the head is associated with the fork by the identity
		// GitHub names, never by an equal SHA. Only an installation on the fork grants access to its deliveries.
		if pullRequest.Head.Repository != nil && pullRequest.Head.Repository.ID != subject.RepositoryID {
			fork := store.GitHubRepository{ID: pullRequest.Head.Repository.ID, Name: pullRequest.Head.Repository.FullName}
			revisions = append(revisions, store.GitHubHeadRevision{Repository: fork, SHA: pullRequest.Head.SHA})
			access, err := p.forkAccess(ctx, *pullRequest.Head.Repository)
			if err != nil {
				return subjectObservation{}, err
			}
			if access != nil {
				if err := p.state.Grant(ctx, *access, fork.Name); err != nil {
					return subjectObservation{}, err
				}
			}
		}
		return subjectObservation{
			revisions: revisions,
			pullRequest: &store.GitHubPullRequestObservation{
				BaseRef: pullRequest.BaseRef, HeadSHA: pullRequest.Head.SHA, BaseSHA: pullRequest.BaseSHA,
				State: pullRequest.State, Mergeability: pullRequest.Mergeability, MergeCommit: pullRequest.MergeCommit,
			},
		}, nil
	case store.IssueSubject:
		return subjectObservation{}, p.client.CheckIssue(ctx, name, int(s.Number), installation)
	case store.BranchSubject:
		sha, found, err := p.client.Branch(ctx, name, s.Name, installation)
		if err != nil || !found {
			return subjectObservation{}, err
		}
		return subjectObservation{revisions: []store.GitHubHeadRevision{{Repository: here, SHA: sha}}}, nil
	case store.CommitSubject:
		if err := p.client.CheckCommit(ctx, name, s.SHA, installation); err != nil {
			return subjectObservation{}, err
		}
		return subjectObservation{revisions: []store.GitHubHeadRevision{{Repository: here, SHA: s.SHA}}}, nil
	default:
		return subjectObservation{}, fmt.Errorf("unsupported GitHub subject %T", s)
	}
}

// refreshFailed records a failed refresh on its shared row and returns the deferral that ends the reconcile until
// the row may be refreshed again. An error that is none of the client's typed failures is returned as it is, and
// the lease runs out.
func (p *Provider) refreshFailed(ctx context.Context, lease store.GitHubRefreshLease, cause error) error {
	failure, ok := classify(cause)
	if !ok {
		return cause
	}
	until, err := p.state.Fail(ctx, lease, failure.kind, failure.message, failure.retry)
	if err != nil {
		return err
	}
	slog.WarnContext(ctx, "GitHub shared refresh failed", "key", lease.Key, "cause", failure.message,
		"retry_seconds", failure.retry.Seconds())
	return &store.RefreshDeferredError{Until: until}
}

// refreshFailure is how a refresh's error shows on the shared row: the message is safe to show, never an upstream
// body or a token.
type refreshFailure struct {
	kind    store.SourceFailureKind
	message string
	retry   time.Duration
}

// classify sorts the client's typed errors, and reports false for any other error: a caller that went away, or a
// bug.
func classify(err error) (refreshFailure, bool) {
	var limited *RateLimitedError
	var denied *AccessError
	var changed *SourceChangedError
	var status *StatusError
	var transport *TransportError
	var invalid *invalidResponseError
	switch {
	case errors.As(err, &limited):
		return refreshFailure{store.FailureRateLimited, err.Error(), time.Duration(limited.RetrySeconds) * time.Second}, true
	case errors.As(err, &denied):
		return refreshFailure{store.FailureAccessDenied, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &changed):
		return refreshFailure{store.FailureSourceChanged, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &status):
		kind := store.FailureProcessingError
		if status.StatusCode >= 500 {
			kind = store.FailureUnavailable
		}
		return refreshFailure{kind, err.Error(), worker.DefaultRetry}, true
	case errors.As(err, &transport):
		return refreshFailure{store.FailureUnavailable, "GitHub request failed", worker.DefaultRetry}, true
	case errors.As(err, &invalid):
		return refreshFailure{store.FailureProcessingError, "GitHub returned an invalid response", worker.DefaultRetry}, true
	}
	return refreshFailure{}, false
}
