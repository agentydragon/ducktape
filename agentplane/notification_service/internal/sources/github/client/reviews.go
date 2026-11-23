package client

import (
	"context"
	"errors"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/google/go-github/v88/github"
)

// PullRequestReviews reads submitted and dismissed reviews for one pull request. Pending reviews are not events
// and are omitted. GitHub returns review records in chronological order; the client preserves that order.
func (c *Client) PullRequestReviews(ctx context.Context, repository string, number int, installationID int64) ([]githubmodel.ReviewObservation, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if number <= 0 {
		return nil, errors.New("pull request number must be above 0")
	}

	observations := make([]githubmodel.ReviewObservation, 0)
	for page := 1; ; {
		result, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (resourcePage[[]*github.PullRequestReview], *github.Response, error) {
			reviews, response, err := client.PullRequests.ListReviews(ctx, owner, name, number, &github.ListOptions{
				PerPage: pageSize,
				Page:    page,
			})
			return resourcePage[[]*github.PullRequestReview]{value: reviews, response: response}, response, err
		})
		if err != nil {
			return nil, err
		}
		if result.response == nil {
			return nil, &InvalidResponseError{Reason: "pull request reviews response is missing"}
		}
		for _, review := range result.value {
			observation, err := ReviewObservation(review)
			if err != nil {
				return nil, err
			}
			if observation != nil {
				observations = append(observations, *observation)
			}
		}
		if result.response.NextPage == 0 {
			return observations, nil
		}
		if result.response.NextPage <= page {
			return nil, &InvalidResponseError{Reason: "pull request reviews pagination did not advance"}
		}
		page = result.response.NextPage
	}
}

// ReviewObservation normalizes the review fields shared by the REST list and pull_request_review webhook. It
// deliberately preserves review.CommitID rather than matching it against the pull request's current head.
func ReviewObservation(review *github.PullRequestReview) (*githubmodel.ReviewObservation, error) {
	if review == nil {
		return nil, &InvalidResponseError{Reason: "pull request review is missing"}
	}

	var state githubmodel.ReviewState
	switch review.GetState() {
	case "PENDING", "pending":
		return nil, nil
	case "APPROVED", "approved":
		state = githubmodel.ReviewApproved
	case "CHANGES_REQUESTED", "changes_requested":
		state = githubmodel.ReviewChangesRequested
	case "COMMENTED", "commented":
		state = githubmodel.ReviewCommented
	case "DISMISSED", "dismissed":
		state = githubmodel.ReviewDismissed
	default:
		return nil, &InvalidResponseError{Reason: "pull request review state is invalid"}
	}

	if review.GetID() <= 0 {
		return nil, &InvalidResponseError{Reason: "pull request review id is missing or invalid"}
	}
	reviewer := review.GetUser()
	if reviewer == nil || reviewer.GetID() <= 0 || reviewer.GetLogin() == "" {
		return nil, &InvalidResponseError{Reason: "pull request review reviewer identity is missing or invalid"}
	}
	commitSHA := review.GetCommitID()
	if !githubmodel.IsCommitSHA(commitSHA) {
		return nil, &InvalidResponseError{Reason: "pull request review commit SHA is missing or invalid"}
	}

	return &githubmodel.ReviewObservation{
		ID:          review.GetID(),
		Reviewer:    githubmodel.Reviewer{ID: reviewer.GetID(), Login: reviewer.GetLogin()},
		State:       state,
		CommitSHA:   commitSHA,
		SubmittedAt: timestampTime(review.SubmittedAt),
		Body:        review.GetBody(),
		HTMLURL:     review.GetHTMLURL(),
	}, nil
}
