package github

import (
	"cmp"
	"context"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/go-github/v88/github"
)

const ciPageSize = 100

type ciPage[T any] struct {
	value    T
	response *github.Response
}

// CommitCI reads the latest check runs and the combined status contexts for an immutable commit SHA.
func (c *Client) CommitCI(ctx context.Context, repository, sha string, installationID int64) ([]store.GitHubCIObservation, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if !validGitSHA(sha) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	observations, err := c.commitCheckRuns(ctx, owner, name, sha, installationID)
	if err != nil {
		return nil, err
	}
	statuses, err := c.commitStatuses(ctx, owner, name, sha, installationID)
	if err != nil {
		return nil, err
	}
	observations = append(observations, statuses...)
	slices.SortStableFunc(observations, func(a, b store.GitHubCIObservation) int {
		if order := cmp.Compare(a.Event, b.Event); order != 0 {
			return order
		}
		if order := cmp.Compare(a.Name, b.Name); order != 0 {
			return order
		}
		return cmp.Compare(a.ID, b.ID)
	})
	return observations, nil
}

func (c *Client) commitCheckRuns(ctx context.Context, owner, repository, sha string, installationID int64) ([]store.GitHubCIObservation, error) {
	var observations []store.GitHubCIObservation
	for page := 1; ; {
		result, err := callAsCIInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (ciPage[*github.ListCheckRunsResults], *github.Response, error) {
			value, response, err := client.Checks.ListCheckRunsForRef(ctx, owner, repository, sha, &github.ListCheckRunsOptions{
				Filter: github.Ptr("latest"),
				ListOptions: github.ListOptions{
					PerPage: ciPageSize,
					Page:    page,
				},
			})
			return ciPage[*github.ListCheckRunsResults]{value: value, response: response}, response, err
		})
		if err != nil {
			return nil, err
		}
		results, response := result.value, result.response
		if results == nil || response == nil {
			return nil, &invalidResponseError{reason: "check runs response is missing"}
		}
		for _, checkRun := range results.CheckRuns {
			observation, err := checkRunObservation(checkRun, sha)
			if err != nil {
				return nil, err
			}
			observations = append(observations, observation)
		}
		if response.NextPage == 0 {
			return observations, nil
		}
		if response.NextPage <= page {
			return nil, &invalidResponseError{reason: "check runs pagination did not advance"}
		}
		page = response.NextPage
	}
}

func checkRunObservation(checkRun *github.CheckRun, sha string) (store.GitHubCIObservation, error) {
	if checkRun == nil {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run is missing"}
	}
	if checkRun.GetHeadSHA() == "" {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run head SHA is missing"}
	}
	if checkRun.GetHeadSHA() != sha {
		return store.GitHubCIObservation{}, &SourceChangedError{Reason: "GitHub check run refers to a different commit"}
	}
	if checkRun.GetID() <= 0 {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run id is missing or invalid"}
	}
	if checkRun.GetName() == "" {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run name is missing"}
	}
	status := checkRun.GetStatus()
	action, ok := checkRunAction(status)
	if !ok {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run status is invalid"}
	}
	if checkRun.Conclusion != nil && !validCheckConclusion(*checkRun.Conclusion) {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "check run conclusion is invalid"}
	}
	if status == "completed" && checkRun.Conclusion == nil {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "completed check run conclusion is missing"}
	}
	var checkAppID int64
	if checkRun.App != nil {
		checkAppID = checkRun.App.GetID()
	}

	return store.GitHubCIObservation{
		HeadSHA:     sha,
		Event:       store.EventCheckRun,
		Action:      &action,
		CheckAppID:  checkAppID,
		ID:          checkRun.GetID(),
		Name:        checkRun.GetName(),
		Status:      status,
		Conclusion:  checkRun.Conclusion,
		DetailsURL:  checkRun.GetDetailsURL(),
		StartedAt:   timestampTime(checkRun.StartedAt),
		CompletedAt: timestampTime(checkRun.CompletedAt),
	}, nil
}

func checkRunAction(status string) (string, bool) {
	switch status {
	case "queued", "waiting", "pending", "requested":
		return "created", true
	case "in_progress":
		return "in_progress", true
	case "completed":
		return "completed", true
	default:
		return "", false
	}
}

func validCheckConclusion(conclusion string) bool {
	switch conclusion {
	case "success", "failure", "neutral", "cancelled", "timed_out", "action_required", "stale", "skipped", "startup_failure":
		return true
	default:
		return false
	}
}

func (c *Client) commitStatuses(ctx context.Context, owner, repository, sha string, installationID int64) ([]store.GitHubCIObservation, error) {
	var observations []store.GitHubCIObservation
	for page := 1; ; {
		result, err := callAsCIInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (ciPage[*github.CombinedStatus], *github.Response, error) {
			value, response, err := client.Repositories.GetCombinedStatus(ctx, owner, repository, sha, &github.ListOptions{
				PerPage: ciPageSize,
				Page:    page,
			})
			return ciPage[*github.CombinedStatus]{value: value, response: response}, response, err
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
			observations = append(observations, observation)
		}
		if response.NextPage == 0 {
			return observations, nil
		}
		if response.NextPage <= page {
			return nil, &invalidResponseError{reason: "combined status pagination did not advance"}
		}
		page = response.NextPage
	}
}

func statusObservation(status *github.RepoStatus, sha string) (store.GitHubCIObservation, error) {
	if status == nil || status.GetID() <= 0 {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "commit status id is missing or invalid"}
	}
	if status.GetContext() == "" {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "commit status context is missing"}
	}
	if !validCommitStatus(status.GetState()) {
		return store.GitHubCIObservation{}, &invalidResponseError{reason: "commit status state is invalid"}
	}
	return store.GitHubCIObservation{
		HeadSHA:    sha,
		Event:      store.EventStatus,
		ID:         status.GetID(),
		Name:       status.GetContext(),
		Status:     status.GetState(),
		DetailsURL: status.GetTargetURL(),
		UpdatedAt:  timestampTime(status.UpdatedAt),
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

func validCommitStatus(status string) bool {
	switch status {
	case "error", "failure", "pending", "success":
		return true
	default:
		return false
	}
}

func timestampTime(timestamp *github.Timestamp) *time.Time {
	if timestamp == nil {
		return nil
	}
	value := timestamp.Time.UTC()
	return &value
}
