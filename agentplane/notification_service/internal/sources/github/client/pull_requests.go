package client

import (
	"context"
	"errors"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/google/go-github/v88/github"
)

// PullRequest pairs the observed state with the head repository. GitHub omits the latter for a deleted fork.
type PullRequest struct {
	githubmodel.PullRequestObservation
	HeadRepository *githubmodel.Repository
}

// PullRequest reads a pull request as an installation.
func (c *Client) PullRequest(ctx context.Context, repository string, number int, installationID int64) (PullRequest, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return PullRequest{}, err
	}
	pullRequest, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.PullRequest, *github.Response, error) {
		return client.PullRequests.Get(ctx, owner, name, number)
	})
	if err != nil {
		return PullRequest{}, err
	}
	if pullRequest == nil || pullRequest.Number == nil {
		return PullRequest{}, &InvalidResponseError{Reason: "pull request number is missing"}
	}
	if *pullRequest.Number != number {
		return PullRequest{}, &SourceChangedError{Reason: "GitHub pull request identity changed"}
	}
	if pullRequest.State == nil {
		return PullRequest{}, &InvalidResponseError{Reason: "pull request state is missing"}
	}
	if pullRequest.Merged == nil {
		return PullRequest{}, &InvalidResponseError{Reason: "pull request merged state is missing"}
	}
	var state githubmodel.PullRequestState
	switch *pullRequest.State {
	case "open":
		if *pullRequest.Merged {
			return PullRequest{}, &InvalidResponseError{Reason: "open pull request is marked merged"}
		}
		state = githubmodel.PullRequestOpen
	case "closed":
		if *pullRequest.Merged {
			state = githubmodel.PullRequestMerged
		} else {
			state = githubmodel.PullRequestClosed
		}
	default:
		return PullRequest{}, &InvalidResponseError{Reason: "pull request state is invalid"}
	}
	base := pullRequest.GetBase()
	head := pullRequest.GetHead()
	if head.GetSHA() == "" {
		return PullRequest{}, &InvalidResponseError{Reason: "pull request head is missing"}
	}
	observation := PullRequest{PullRequestObservation: githubmodel.PullRequestObservation{
		HeadSHA: head.GetSHA(), BaseRef: base.GetRef(), State: state, Mergeability: githubmodel.MergeabilityUnknown,
	}}
	if head.Repo != nil {
		fork, err := repositoryFrom(head.Repo)
		if err != nil {
			return PullRequest{}, err
		}
		observation.HeadRepository = &fork
	}
	if state != githubmodel.PullRequestOpen {
		if base.GetRef() == "" {
			return PullRequest{}, &InvalidResponseError{Reason: "pull request base ref is missing"}
		}
		if base.SHA == nil || *base.SHA == "" {
			return PullRequest{}, &InvalidResponseError{Reason: "pull request base sha is missing"}
		}
		observation.BaseSHA = *base.SHA
		if pullRequest.Mergeable != nil {
			if *pullRequest.Mergeable {
				observation.Mergeability = githubmodel.Mergeable
			} else {
				observation.Mergeability = githubmodel.Conflicting
			}
		}
		return observation, nil
	}
	if observation.BaseRef == "" {
		return observation, nil
	}

	var testCommit *github.Commit
	if pullRequest.GetMergeable() && pullRequest.MergeCommitSHA != nil && githubmodel.IsCommitSHA(*pullRequest.MergeCommitSHA) {
		testCommit, err = c.pullRequestTestCommit(ctx, owner, name, *pullRequest.MergeCommitSHA, installationID)
		if err != nil {
			return PullRequest{}, err
		}
	}
	baseSHA, found, err := c.Branch(ctx, repository, observation.BaseRef, installationID)
	if err != nil {
		return PullRequest{}, err
	}
	if !found || !githubmodel.IsCommitSHA(baseSHA) {
		return observation, nil
	}
	observation.BaseSHA = baseSHA
	switch {
	case pullRequest.Mergeable == nil:
		return observation, nil
	case !*pullRequest.Mergeable:
		observation.Mergeability = githubmodel.Conflicting
		return observation, nil
	}
	if testCommit == nil || testCommit.GetSHA() != *pullRequest.MergeCommitSHA || !githubmodel.IsCommitSHA(testCommit.GetSHA()) ||
		len(testCommit.Parents) != 2 || testCommit.Parents[0] == nil || testCommit.Parents[1] == nil {
		return observation, nil
	}
	first, second := testCommit.Parents[0].GetSHA(), testCommit.Parents[1].GetSHA()
	if !githubmodel.IsCommitSHA(first) || !githubmodel.IsCommitSHA(second) ||
		!((first == baseSHA && second == observation.HeadSHA) || (second == baseSHA && first == observation.HeadSHA)) {
		return observation, nil
	}
	observation.Mergeability = githubmodel.Mergeable
	observation.MergeCommit = &githubmodel.MergeCommit{SHA: testCommit.GetSHA(), BaseSHA: baseSHA, HeadSHA: observation.HeadSHA}
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
