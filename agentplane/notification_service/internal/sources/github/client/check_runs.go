package client

import (
	"context"
	"errors"
	"sort"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/google/go-github/v88/github"
)

// CheckRuns reads the latest check runs for an immutable commit SHA.
func (c *Client) CheckRuns(ctx context.Context, repository, sha string, installationID int64) ([]githubmodel.CheckRun, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if !githubmodel.IsCommitSHA(sha) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	runs := make([]githubmodel.CheckRun, 0)
	for page := 1; ; {
		result, err := callAsCheckRunsInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (resourcePage[*github.ListCheckRunsResults], *github.Response, error) {
			value, response, err := client.Checks.ListCheckRunsForRef(ctx, owner, name, sha, &github.ListCheckRunsOptions{
				Filter: github.Ptr("latest"),
				ListOptions: github.ListOptions{
					PerPage: pageSize,
					Page:    page,
				},
			})
			return resourcePage[*github.ListCheckRunsResults]{value: value, response: response}, response, err
		})
		if err != nil {
			return nil, err
		}
		results, response := result.value, result.response
		if results == nil || response == nil {
			return nil, &InvalidResponseError{Reason: "check runs response is missing"}
		}
		for _, checkRun := range results.CheckRuns {
			run, err := CheckRunObservation(checkRun, sha)
			if err != nil {
				return nil, err
			}
			runs = append(runs, run)
		}
		if response.NextPage == 0 {
			sort.SliceStable(runs, func(i, j int) bool { return runs[i].ID < runs[j].ID })
			return runs, nil
		}
		if response.NextPage <= page {
			return nil, &InvalidResponseError{Reason: "check runs pagination did not advance"}
		}
		page = response.NextPage
	}
}

func CheckRunObservation(checkRun *github.CheckRun, sha string) (githubmodel.CheckRun, error) {
	if checkRun == nil {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run is missing"}
	}
	if checkRun.GetHeadSHA() == "" {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run head SHA is missing"}
	}
	if checkRun.GetHeadSHA() != sha {
		return githubmodel.CheckRun{}, &SourceChangedError{Reason: "GitHub check run refers to a different commit"}
	}
	if checkRun.GetID() <= 0 {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run id is missing or invalid"}
	}
	if checkRun.GetName() == "" {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run name is missing"}
	}
	status, ok := checkRunStatus(checkRun.GetStatus())
	if !ok {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run status is invalid"}
	}
	if checkRun.Conclusion != nil && !githubmodel.IsCheckConclusion(*checkRun.Conclusion) {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run conclusion is invalid"}
	}
	if status == githubmodel.CheckRunCompleted && checkRun.Conclusion == nil {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "completed check run conclusion is missing"}
	}
	if status != githubmodel.CheckRunCompleted && checkRun.Conclusion != nil {
		return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "non-completed check run has a conclusion"}
	}
	var appID int64
	if checkRun.App != nil {
		appID = checkRun.App.GetID()
		if appID < 0 {
			return githubmodel.CheckRun{}, &InvalidResponseError{Reason: "check run App ID is invalid"}
		}
	}
	return githubmodel.CheckRun{
		HeadSHA:     sha,
		ID:          checkRun.GetID(),
		AppID:       appID,
		Name:        checkRun.GetName(),
		Status:      status,
		Conclusion:  checkRun.Conclusion,
		DetailsURL:  checkRun.GetDetailsURL(),
		StartedAt:   timestampTime(checkRun.StartedAt),
		CompletedAt: timestampTime(checkRun.CompletedAt),
	}, nil
}

func checkRunStatus(status string) (githubmodel.CheckRunStatus, bool) {
	switch status {
	case string(githubmodel.CheckRunQueued), string(githubmodel.CheckRunWaiting), string(githubmodel.CheckRunPending),
		string(githubmodel.CheckRunRequested), string(githubmodel.CheckRunInProgress), string(githubmodel.CheckRunCompleted):
		return githubmodel.CheckRunStatus(status), true
	default:
		return "", false
	}
}
