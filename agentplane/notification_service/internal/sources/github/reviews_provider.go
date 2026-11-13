package github

import (
	"context"
	"errors"
	"slices"
	"strconv"
	"time"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func reviewsSelected(source model.GitHubSource) bool {
	if _, ok := source.Subject.(model.PullRequestSubject); !ok {
		return false
	}
	return slices.ContainsFunc(source.Filters(), func(filter model.EventFilter) bool {
		return filter.Event == model.EventPullRequestReview
	})
}

// Review API failures have their own durable deadline and do not stop ordinary webhook or lifecycle delivery.
func (p *Provider) refreshReviews(ctx context.Context, subject githubstate.SubjectKey, fence githubstate.AccessFence) (*time.Time, error) {
	key := githubstate.ReviewRefreshKey{AppID: p.appID, InstallationID: fence.Key.InstallationID, Subject: subject}
	lease, err := p.state.Acquire(ctx, key)
	var deferred *githubstate.RefreshDeferredError
	if errors.As(err, &deferred) {
		return &deferred.Until, nil
	}
	if err != nil {
		return nil, err
	}
	if lease != nil {
		name, err := p.state.RepositoryName(ctx, subject.RepositoryID)
		if err != nil {
			return nil, err
		}
		number, err := strconv.ParseInt(subject.Key, 10, 64)
		if err != nil {
			return nil, err
		}
		reviews, err := p.client.PullRequestReviews(ctx, name, int(number), fence.Key.InstallationID)
		if err != nil {
			err = p.refreshFailed(ctx, *lease, err)
		} else {
			err = p.state.CompleteReviews(ctx, *lease, []githubstate.AccessFence{fence}, reviews)
		}
		if errors.As(err, &deferred) {
			return &deferred.Until, nil
		}
		if err != nil {
			return nil, err
		}
	}
	state, err := p.state.ReviewsState(ctx, key)
	return state.RetryAt, err
}
