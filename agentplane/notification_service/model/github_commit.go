package model

import (
	"time"
)

// GitHubCheckRunStatus uses GitHub's CheckRunStatus vocabulary.
type GitHubCheckRunStatus string

const (
	CheckRunQueued     GitHubCheckRunStatus = "queued"
	CheckRunWaiting    GitHubCheckRunStatus = "waiting"
	CheckRunPending    GitHubCheckRunStatus = "pending"
	CheckRunRequested  GitHubCheckRunStatus = "requested"
	CheckRunInProgress GitHubCheckRunStatus = "in_progress"
	CheckRunCompleted  GitHubCheckRunStatus = "completed"
)

// GitHubCheckRun is one native check-run fact, whether first found by a webhook or by subscription repair.
type GitHubCheckRun struct {
	RepositoryID int64                `json:"repositoryId"`
	HeadSHA      string               `json:"headSha"`
	ID           int64                `json:"id"`
	AppID        int64                `json:"appId,omitempty"`
	Name         string               `json:"name"`
	Status       GitHubCheckRunStatus `json:"status"`
	Conclusion   *string              `json:"conclusion,omitempty"`
	DetailsURL   string               `json:"detailsUrl"`
	StartedAt    *time.Time           `json:"startedAt,omitempty"`
	CompletedAt  *time.Time           `json:"completedAt,omitempty"`
}

// GitHubCommitStatusState uses GitHub's CommitStatusState vocabulary.
type GitHubCommitStatusState string

const (
	CommitStatusError   GitHubCommitStatusState = "error"
	CommitStatusFailure GitHubCommitStatusState = "failure"
	CommitStatusPending GitHubCommitStatusState = "pending"
	CommitStatusSuccess GitHubCommitStatusState = "success"
)

// GitHubCommitStatus is one native commit-status fact, whether first found by a webhook or by subscription repair.
type GitHubCommitStatus struct {
	RepositoryID int64                   `json:"repositoryId"`
	SHA          string                  `json:"sha"`
	ID           int64                   `json:"id"`
	Context      string                  `json:"context"`
	State        GitHubCommitStatusState `json:"state"`
	TargetURL    string                  `json:"targetUrl"`
	UpdatedAt    *time.Time              `json:"updatedAt,omitempty"`
}

// NormalizeGitHubCheckRun validates and normalizes one native check-run fact.
func NormalizeGitHubCheckRun(run GitHubCheckRun) (GitHubCheckRun, error) {
	if run.RepositoryID <= 0 || !IsCommitSHA(run.HeadSHA) || run.ID <= 0 {
		return GitHubCheckRun{}, Invalid("check run must name a repository, commit SHA and ID")
	}
	if run.AppID < 0 {
		return GitHubCheckRun{}, Invalid("check run App ID must not be negative")
	}
	if err := CheckText("check run name", run.Name, 1, 512); err != nil {
		return GitHubCheckRun{}, err
	}
	if err := CheckText("check run details URL", run.DetailsURL, 0, 4096); err != nil {
		return GitHubCheckRun{}, err
	}
	switch run.Status {
	case CheckRunQueued, CheckRunWaiting, CheckRunPending, CheckRunRequested, CheckRunInProgress, CheckRunCompleted:
	default:
		return GitHubCheckRun{}, Invalid("check run status is invalid")
	}
	if run.Conclusion != nil {
		if err := CheckText("check run conclusion", *run.Conclusion, 1, 128); err != nil {
			return GitHubCheckRun{}, err
		}
		if run.Status != CheckRunCompleted {
			return GitHubCheckRun{}, Invalid("non-completed check run cannot have a conclusion")
		}
	}
	if run.Status == CheckRunCompleted && run.Conclusion == nil {
		return GitHubCheckRun{}, Invalid("completed check run must have a conclusion")
	}
	if run.StartedAt != nil {
		value := run.StartedAt.UTC().Truncate(time.Microsecond)
		run.StartedAt = &value
	}
	if run.CompletedAt != nil {
		value := run.CompletedAt.UTC().Truncate(time.Microsecond)
		run.CompletedAt = &value
	}
	return run, nil
}

// NormalizeGitHubCommitStatus validates and normalizes one native commit-status fact.
func NormalizeGitHubCommitStatus(status GitHubCommitStatus) (GitHubCommitStatus, error) {
	if status.RepositoryID <= 0 || !IsCommitSHA(status.SHA) || status.ID <= 0 {
		return GitHubCommitStatus{}, Invalid("commit status must name a repository, commit SHA and ID")
	}
	if err := CheckText("commit status context", status.Context, 1, 512); err != nil {
		return GitHubCommitStatus{}, err
	}
	if err := CheckText("commit status target URL", status.TargetURL, 0, 4096); err != nil {
		return GitHubCommitStatus{}, err
	}
	switch status.State {
	case CommitStatusError, CommitStatusFailure, CommitStatusPending, CommitStatusSuccess:
	default:
		return GitHubCommitStatus{}, Invalid("commit status state is invalid")
	}
	if status.UpdatedAt != nil {
		value := status.UpdatedAt.UTC().Truncate(time.Microsecond)
		status.UpdatedAt = &value
	}
	return status, nil
}
