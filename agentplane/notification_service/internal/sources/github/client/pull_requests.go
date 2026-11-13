package client

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/go-github/v88/github"
)

// PullRequest reads a pull request as an installation.
func (c *Client) PullRequest(ctx context.Context, repository string, number int, installationID int64) (PullRequestObservation, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return PullRequestObservation{}, err
	}
	pullRequest, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.PullRequest, *github.Response, error) {
		return client.PullRequests.Get(ctx, owner, name, number)
	})
	if err != nil {
		return PullRequestObservation{}, err
	}
	if pullRequest == nil || pullRequest.Number == nil {
		return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request number is missing"}
	}
	if *pullRequest.Number != number {
		return PullRequestObservation{}, &SourceChangedError{Reason: "GitHub pull request identity changed"}
	}
	if pullRequest.State == nil {
		return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request state is missing"}
	}
	if pullRequest.Merged == nil {
		return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request merged state is missing"}
	}
	var state model.GitHubPullRequestState
	switch *pullRequest.State {
	case "open":
		if *pullRequest.Merged {
			return PullRequestObservation{}, &InvalidResponseError{Reason: "open pull request is marked merged"}
		}
		state = model.GitHubPullRequestOpen
	case "closed":
		if *pullRequest.Merged {
			state = model.GitHubPullRequestMerged
		} else {
			state = model.GitHubPullRequestClosed
		}
	default:
		return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request state is invalid"}
	}
	base := pullRequest.GetBase()
	head := pullRequest.GetHead()
	if head.GetSHA() == "" {
		return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request head is missing"}
	}
	revision := Revision{SHA: head.GetSHA()}
	if head.Repo != nil {
		fork, err := repositoryFrom(head.Repo)
		if err != nil {
			return PullRequestObservation{}, err
		}
		revision.Repository = &fork
	}
	var mergeability model.GitHubMergeableState
	switch {
	case pullRequest.Mergeable == nil:
		mergeability = model.GitHubMergeabilityUnknown
	case *pullRequest.Mergeable:
		mergeability = model.GitHubMergeable
	default:
		mergeability = model.GitHubConflicting
	}
	if state != model.GitHubPullRequestOpen {
		if base.GetRef() == "" {
			return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request base ref is missing"}
		}
		if base.SHA == nil || *base.SHA == "" {
			return PullRequestObservation{}, &InvalidResponseError{Reason: "pull request base sha is missing"}
		}
		return PullRequestObservation{
			Head: revision, BaseRef: base.GetRef(), BaseSHA: *base.SHA, State: state, Mergeability: mergeability,
		}, nil
	}

	observation := PullRequestObservation{
		Head: revision, BaseRef: base.GetRef(), State: state, Mergeability: model.GitHubMergeabilityUnknown,
	}
	if observation.BaseRef == "" {
		return observation, nil
	}

	var testCommit *github.Commit
	if pullRequest.GetMergeable() && pullRequest.MergeCommitSHA != nil && model.IsCommitSHA(*pullRequest.MergeCommitSHA) {
		testCommit, err = c.pullRequestTestCommit(ctx, owner, name, *pullRequest.MergeCommitSHA, installationID)
		if err != nil {
			return PullRequestObservation{}, err
		}
	}
	baseSHA, found, err := c.Branch(ctx, repository, observation.BaseRef, installationID)
	if err != nil {
		return PullRequestObservation{}, err
	}
	if !found || !model.IsCommitSHA(baseSHA) {
		return observation, nil
	}
	observation.BaseSHA = baseSHA
	switch {
	case pullRequest.Mergeable == nil:
		return observation, nil
	case !*pullRequest.Mergeable:
		observation.Mergeability = model.GitHubConflicting
		return observation, nil
	}
	if testCommit == nil || testCommit.GetSHA() != *pullRequest.MergeCommitSHA || !model.IsCommitSHA(testCommit.GetSHA()) ||
		len(testCommit.Parents) != 2 || testCommit.Parents[0] == nil || testCommit.Parents[1] == nil {
		return observation, nil
	}
	first, second := testCommit.Parents[0].GetSHA(), testCommit.Parents[1].GetSHA()
	if !model.IsCommitSHA(first) || !model.IsCommitSHA(second) ||
		!((first == baseSHA && second == revision.SHA) || (second == baseSHA && first == revision.SHA)) {
		return observation, nil
	}
	observation.Mergeability = model.GitHubMergeable
	observation.MergeCommit = &model.GitHubMergeCommit{SHA: testCommit.GetSHA(), BaseSHA: baseSHA, HeadSHA: revision.SHA}
	return observation, nil
}

// pullRequestTestCommit reads the test merge as an installation. A missing commit is an unavailable proof, not a
// failure to read the pull request; other errors still reach the caller.
func (c *Client) pullRequestTestCommit(ctx context.Context, owner, repository, sha string, installationID int64) (*github.Commit, error) {
	commit, err := callAsInstallation(ctx, c, installationID, true, func(ctx context.Context, client *github.Client) (*github.Commit, *github.Response, error) {
		return client.Git.GetCommit(ctx, owner, repository, sha)
	})
	if errors.Is(err, errMissing) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return commit, nil
}
