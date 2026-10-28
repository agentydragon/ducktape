package github

import (
	"context"
	"errors"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/go-github/v88/github"
)

const commitStatusesPageSize = 100

// CommitStatuses reads all status contexts for an immutable commit SHA.
func (c *Client) CommitStatuses(ctx context.Context, repository, sha string, installationID int64) ([]store.GitHubCommitStatus, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if !validGitSHA(sha) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	statuses := make([]store.GitHubCommitStatus, 0)
	for page := 1; ; {
		result, err := callAsCommitStatusesInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (resourcePage[*github.CombinedStatus], *github.Response, error) {
			value, response, err := client.Repositories.GetCombinedStatus(ctx, owner, name, sha, &github.ListOptions{
				PerPage: commitStatusesPageSize,
				Page:    page,
			})
			return resourcePage[*github.CombinedStatus]{value: value, response: response}, response, err
		})
		if err != nil {
			return nil, err
		}
		combined, response := result.value, result.response
		if combined == nil || response == nil {
			return nil, &invalidResponseError{reason: "combined status response is missing"}
		}
		if combined.GetSHA() == "" {
			return nil, &invalidResponseError{reason: "combined status SHA is missing"}
		}
		if combined.GetSHA() != sha {
			return nil, &SourceChangedError{Reason: "GitHub combined status refers to a different commit"}
		}
		if !validCombinedStatus(combined.GetState()) {
			return nil, &invalidResponseError{reason: "combined status state is invalid"}
		}
		for _, status := range combined.Statuses {
			observation, err := statusObservation(status, sha)
			if err != nil {
				return nil, err
			}
			statuses = append(statuses, observation)
		}
		if response.NextPage == 0 {
			sort.SliceStable(statuses, func(i, j int) bool { return statuses[i].ID < statuses[j].ID })
			return statuses, nil
		}
		if response.NextPage <= page {
			return nil, &invalidResponseError{reason: "combined status pagination did not advance"}
		}
		page = response.NextPage
	}
}

func statusObservation(status *github.RepoStatus, sha string) (store.GitHubCommitStatus, error) {
	if status == nil || status.GetID() <= 0 {
		return store.GitHubCommitStatus{}, &invalidResponseError{reason: "commit status id is missing or invalid"}
	}
	if status.GetContext() == "" {
		return store.GitHubCommitStatus{}, &invalidResponseError{reason: "commit status context is missing"}
	}
	state, ok := commitStatusState(status.GetState())
	if !ok {
		return store.GitHubCommitStatus{}, &invalidResponseError{reason: "commit status state is invalid"}
	}
	return store.GitHubCommitStatus{
		SHA:       sha,
		ID:        status.GetID(),
		Context:   status.GetContext(),
		State:     state,
		TargetURL: status.GetTargetURL(),
		UpdatedAt: timestampTime(status.UpdatedAt),
	}, nil
}

func commitStatusState(state string) (store.GitHubCommitStatusState, bool) {
	switch state {
	case "error":
		return store.CommitStatusError, true
	case "failure":
		return store.CommitStatusFailure, true
	case "pending":
		return store.CommitStatusPending, true
	case "success":
		return store.CommitStatusSuccess, true
	default:
		return "", false
	}
}

func validCombinedStatus(status string) bool {
	switch status {
	case "failure", "pending", "success":
		return true
	default:
		return false
	}
}
