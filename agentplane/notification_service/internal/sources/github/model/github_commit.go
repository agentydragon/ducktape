package model

import (
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// CheckRunStatus uses GitHub's CheckRunStatus vocabulary.
type CheckRunStatus string

const (
	CheckRunQueued     CheckRunStatus = "queued"
	CheckRunWaiting    CheckRunStatus = "waiting"
	CheckRunPending    CheckRunStatus = "pending"
	CheckRunRequested  CheckRunStatus = "requested"
	CheckRunInProgress CheckRunStatus = "in_progress"
	CheckRunCompleted  CheckRunStatus = "completed"
)

// IsCheckConclusion reports whether value is a conclusion GitHub gives a completed check run or workflow run.
func IsCheckConclusion(value string) bool {
	switch value {
	case "success", "failure", "neutral", "cancelled", "timed_out", "action_required", "stale", "skipped", "startup_failure":
		return true
	default:
		return false
	}
}

// CheckRun is one native check-run fact, whether first found by a webhook or by subscription repair.
type CheckRun struct {
	RepositoryID int64          `json:"repositoryId"`
	HeadSHA      string         `json:"headSha"`
	ID           int64          `json:"id"`
	AppID        int64          `json:"appId,omitempty"`
	Name         string         `json:"name"`
	Status       CheckRunStatus `json:"status"`
	Conclusion   *string        `json:"conclusion,omitempty"`
	DetailsURL   string         `json:"detailsUrl"`
	StartedAt    *time.Time     `json:"startedAt,omitempty"`
	CompletedAt  *time.Time     `json:"completedAt,omitempty"`
}

// CommitStatusState uses GitHub's CommitStatusState vocabulary.
type CommitStatusState string

const (
	CommitStatusError   CommitStatusState = "error"
	CommitStatusFailure CommitStatusState = "failure"
	CommitStatusPending CommitStatusState = "pending"
	CommitStatusSuccess CommitStatusState = "success"
)

// CommitStatus is one native commit-status fact, whether first found by a webhook or by subscription repair.
type CommitStatus struct {
	RepositoryID int64             `json:"repositoryId"`
	SHA          string            `json:"sha"`
	ID           int64             `json:"id"`
	Context      string            `json:"context"`
	State        CommitStatusState `json:"state"`
	TargetURL    string            `json:"targetUrl"`
	UpdatedAt    *time.Time        `json:"updatedAt,omitempty"`
}

// NormalizeCheckRun validates and normalizes one native check-run fact.
func NormalizeCheckRun(run CheckRun) (CheckRun, error) {
	if run.RepositoryID <= 0 || !IsCommitSHA(run.HeadSHA) || run.ID <= 0 {
		return CheckRun{}, model.Invalid("check run must name a repository, commit SHA and ID")
	}
	if run.AppID < 0 {
		return CheckRun{}, model.Invalid("check run App ID must not be negative")
	}
	if err := model.CheckText("check run name", run.Name, 1, 512); err != nil {
		return CheckRun{}, err
	}
	if err := model.CheckText("check run details URL", run.DetailsURL, 0, 4096); err != nil {
		return CheckRun{}, err
	}
	switch run.Status {
	case CheckRunQueued, CheckRunWaiting, CheckRunPending, CheckRunRequested, CheckRunInProgress, CheckRunCompleted:
	default:
		return CheckRun{}, model.Invalid("check run status is invalid")
	}
	if run.Conclusion != nil {
		if err := model.CheckText("check run conclusion", *run.Conclusion, 1, 128); err != nil {
			return CheckRun{}, err
		}
		if run.Status != CheckRunCompleted {
			return CheckRun{}, model.Invalid("non-completed check run cannot have a conclusion")
		}
	}
	if run.Status == CheckRunCompleted && run.Conclusion == nil {
		return CheckRun{}, model.Invalid("completed check run must have a conclusion")
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

// NormalizeCommitStatus validates and normalizes one native commit-status fact.
func NormalizeCommitStatus(status CommitStatus) (CommitStatus, error) {
	if status.RepositoryID <= 0 || !IsCommitSHA(status.SHA) || status.ID <= 0 {
		return CommitStatus{}, model.Invalid("commit status must name a repository, commit SHA and ID")
	}
	if err := model.CheckText("commit status context", status.Context, 1, 512); err != nil {
		return CommitStatus{}, err
	}
	if err := model.CheckText("commit status target URL", status.TargetURL, 0, 4096); err != nil {
		return CommitStatus{}, err
	}
	switch status.State {
	case CommitStatusError, CommitStatusFailure, CommitStatusPending, CommitStatusSuccess:
	default:
		return CommitStatus{}, model.Invalid("commit status state is invalid")
	}
	if status.UpdatedAt != nil {
		value := status.UpdatedAt.UTC().Truncate(time.Microsecond)
		status.UpdatedAt = &value
	}
	return status, nil
}
