package github

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

func reviewsSelected(source model.GitHubSource) bool {
	if _, ok := source.Subject.(model.PullRequestSubject); !ok {
		return false
	}
	for _, filter := range source.Filters() {
		if filter.Event == model.EventPullRequestReview {
			return true
		}
	}
	return false
}

// Review API failures have their own durable deadline and do not stop ordinary webhook or lifecycle delivery.
func (p *Provider) refreshReviews(ctx context.Context, subject store.GitHubSubjectKey, fence store.GitHubAccessFence) (time.Time, error) {
	key := store.GitHubReviewRefreshKey{AppID: p.appID, InstallationID: fence.Key.InstallationID, Subject: subject}
	lease, err := p.state.Acquire(ctx, key)
	var deferred *store.RefreshDeferredError
	if errors.As(err, &deferred) {
		return deferred.Until, nil
	}
	if err != nil {
		return time.Time{}, err
	}
	if lease != nil {
		name, err := p.state.RepositoryName(ctx, subject.RepositoryID)
		if err != nil {
			return time.Time{}, err
		}
		number, err := strconv.ParseInt(subject.Key, 10, 64)
		if err != nil {
			return time.Time{}, err
		}
		reviews, err := p.client.PullRequestReviews(ctx, name, int(number), fence.Key.InstallationID)
		if err != nil {
			err = p.refreshFailed(ctx, *lease, err)
		} else {
			err = p.state.CompleteReviews(ctx, *lease, []store.GitHubAccessFence{fence}, reviews)
		}
		if errors.As(err, &deferred) {
			return deferred.Until, nil
		}
		if err != nil {
			return time.Time{}, err
		}
	}
	state, err := p.state.ReviewsState(ctx, key)
	return state.RepairAt, err
}
