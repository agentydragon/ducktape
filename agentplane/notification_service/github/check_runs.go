package github

import (
	"context"
	"errors"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/go-github/v88/github"
)

const checkRunsPageSize = 100

// CheckRuns reads the latest check runs for an immutable commit SHA.
func (c *Client) CheckRuns(ctx context.Context, repository, sha string, installationID int64) ([]store.GitHubCheckRun, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return nil, err
	}
	if !validGitSHA(sha) {
		return nil, errors.New("commit SHA must be a full lowercase Git SHA")
	}

	runs := make([]store.GitHubCheckRun, 0)
	for page := 1; ; {
		result, err := callAsCheckRunsInstallation(ctx, c, installationID, func(ctx context.Context, client *github.Client) (resourcePage[*github.ListCheckRunsResults], *github.Response, error) {
			value, response, err := client.Checks.ListCheckRunsForRef(ctx, owner, name, sha, &github.ListCheckRunsOptions{
				Filter: github.Ptr("latest"),
				ListOptions: github.ListOptions{
					PerPage: checkRunsPageSize,
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
			return nil, &invalidResponseError{reason: "check runs response is missing"}
		}
		for _, checkRun := range results.CheckRuns {
			run, err := checkRunObservation(checkRun, sha)
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
			return nil, &invalidResponseError{reason: "check runs pagination did not advance"}
		}
		page = response.NextPage
	}
}

func checkRunObservation(checkRun *github.CheckRun, sha string) (store.GitHubCheckRun, error) {
	if checkRun == nil {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run is missing"}
	}
	if checkRun.GetHeadSHA() == "" {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run head SHA is missing"}
	}
	if checkRun.GetHeadSHA() != sha {
		return store.GitHubCheckRun{}, &SourceChangedError{Reason: "GitHub check run refers to a different commit"}
	}
	if checkRun.GetID() <= 0 {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run id is missing or invalid"}
	}
	if checkRun.GetName() == "" {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run name is missing"}
	}
	status, ok := checkRunStatus(checkRun.GetStatus())
	if !ok {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run status is invalid"}
	}
	if checkRun.Conclusion != nil && !validCheckConclusion(*checkRun.Conclusion) {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run conclusion is invalid"}
	}
	if status == store.CheckRunCompleted && checkRun.Conclusion == nil {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "completed check run conclusion is missing"}
	}
	if status != store.CheckRunCompleted && checkRun.Conclusion != nil {
		return store.GitHubCheckRun{}, &invalidResponseError{reason: "non-completed check run has a conclusion"}
	}
	var appID int64
	if checkRun.App != nil {
		appID = checkRun.App.GetID()
		if appID < 0 {
			return store.GitHubCheckRun{}, &invalidResponseError{reason: "check run App ID is invalid"}
		}
	}
	return store.GitHubCheckRun{
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

func checkRunStatus(status string) (store.GitHubCheckRunStatus, bool) {
	switch status {
	case string(store.CheckRunQueued), string(store.CheckRunWaiting), string(store.CheckRunPending),
		string(store.CheckRunRequested), string(store.CheckRunInProgress), string(store.CheckRunCompleted):
		return store.GitHubCheckRunStatus(status), true
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
