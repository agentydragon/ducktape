package client

import (
	"context"
	"errors"
	"sort"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/google/go-github/v88/github"
)

// CommitStatuses reads all status contexts for an immutable commit SHA.
func (c *Client) CommitStatuses(ctx context.Context, repository, sha string, installationID int64) ([]githubmodel.CommitStatus, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if !githubmodel.IsCommitSHA(sha) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	statuses := make([]githubmodel.CommitStatus, 0)
	for page := 1; ; {
		result, err := callAsCommitStatusesInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (resourcePage[*github.CombinedStatus], *github.Response, error) {
			value, response, err := client.Repositories.GetCombinedStatus(ctx, owner, name, sha, &github.ListOptions{
				PerPage: pageSize,
				Page:    page,
			})
			return resourcePage[*github.CombinedStatus]{value: value, response: response}, response, err
		})
		if err != nil {
			return nil, err
		}
		combined, response := result.value, result.response
		if combined == nil || response == nil {
			return nil, &InvalidResponseError{Reason: "combined status response is missing"}
		}
		if combined.GetSHA() == "" {
			return nil, &InvalidResponseError{Reason: "combined status SHA is missing"}
		}
		if combined.GetSHA() != sha {
			return nil, &SourceChangedError{Reason: "GitHub combined status refers to a different commit"}
		}
		if !validCombinedStatus(combined.GetState()) {
			return nil, &InvalidResponseError{Reason: "combined status state is invalid"}
		}
		for _, status := range combined.Statuses {
			observation, err := CommitStatusObservation(status, sha)
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
			return nil, &InvalidResponseError{Reason: "combined status pagination did not advance"}
		}
		page = response.NextPage
	}
}

func CommitStatusObservation(status *github.RepoStatus, sha string) (githubmodel.CommitStatus, error) {
	if status == nil || status.GetID() <= 0 {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status id is missing or invalid"}
	}
	if status.GetContext() == "" {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status context is missing"}
	}
	state, ok := commitStatusState(status.GetState())
	if !ok {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status state is invalid"}
	}
	return githubmodel.CommitStatus{
		SHA:       sha,
		ID:        status.GetID(),
		Context:   status.GetContext(),
		State:     state,
		TargetURL: status.GetTargetURL(),
		UpdatedAt: timestampTime(status.UpdatedAt),
	}, nil
}

func commitStatusState(state string) (githubmodel.CommitStatusState, bool) {
	switch state {
	case "error":
		return githubmodel.CommitStatusError, true
	case "failure":
		return githubmodel.CommitStatusFailure, true
	case "pending":
		return githubmodel.CommitStatusPending, true
	case "success":
		return githubmodel.CommitStatusSuccess, true
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
