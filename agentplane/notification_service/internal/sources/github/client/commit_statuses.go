package client

import (
	"context"
	"errors"
	"sort"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/google/go-github/v88/github"
)

// CommitStatuses reads all status contexts for an immutable commit SHA.
func (c *Client) CommitStatuses(ctx context.Context, revision githubmodel.HeadRevision, installationID int64) ([]githubmodel.CommitStatus, error) {
	owner, name, err := splitRepository(revision.Repository.Name)
	if err != nil {
		return nil, err
	}
	if !githubmodel.IsCommitSHA(revision.SHA) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	statuses := make([]githubmodel.CommitStatus, 0)
	pages := installationPages(ctx, c, installationID, commitStatusesRead, "combined status", func(ctx context.Context, client *github.Client, opts github.ListOptions) (*github.CombinedStatus, *github.Response, error) {
		return client.Repositories.GetCombinedStatus(ctx, owner, name, revision.SHA, &opts)
	})
	for combined, err := range pages {
		if err != nil {
			return nil, err
		}
		if combined == nil {
			return nil, &InvalidResponseError{Reason: "combined status response is missing"}
		}
		if combined.GetSHA() == "" {
			return nil, &InvalidResponseError{Reason: "combined status SHA is missing"}
		}
		if combined.GetSHA() != revision.SHA {
			return nil, &SourceChangedError{Reason: "GitHub combined status refers to a different commit"}
		}
		if !validCombinedStatus(combined.GetState()) {
			return nil, &InvalidResponseError{Reason: "combined status state is invalid"}
		}
		for _, status := range combined.Statuses {
			observation, err := CommitStatusObservation(status, revision)
			if err != nil {
				return nil, err
			}
			statuses = append(statuses, observation)
		}
	}
	sort.SliceStable(statuses, func(i, j int) bool { return statuses[i].ID < statuses[j].ID })
	return statuses, nil
}

func CommitStatusObservation(status *github.RepoStatus, revision githubmodel.HeadRevision) (githubmodel.CommitStatus, error) {
	if status == nil || status.GetID() <= 0 {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status id is missing or invalid"}
	}
	if status.GetContext() == "" {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status context is missing"}
	}
	state := githubmodel.CommitStatusState(status.GetState())
	if !state.Valid() {
		return githubmodel.CommitStatus{}, &InvalidResponseError{Reason: "commit status state is invalid"}
	}
	return githubmodel.CommitStatus{
		RepositoryID: revision.Repository.ID,
		SHA:          revision.SHA,
		ID:           status.GetID(),
		Context:      status.GetContext(),
		State:        state,
		TargetURL:    status.GetTargetURL(),
		UpdatedAt:    timestampTime(status.UpdatedAt),
	}, nil
}

func validCombinedStatus(status string) bool {
	switch status {
	case "failure", "pending", "success":
		return true
	default:
		return false
	}
}
