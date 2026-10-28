package model

import (
	"time"
)

// GitHubReviewer is the stable identity of a review's author.
type GitHubReviewer struct {
	ID    int64  `json:"id"`
	Login string `json:"login"`
}

// GitHubReviewState is the review state reported by GitHub after submission or dismissal.
type GitHubReviewState string

const (
	ReviewApproved         GitHubReviewState = "APPROVED"
	ReviewChangesRequested GitHubReviewState = "CHANGES_REQUESTED"
	ReviewCommented        GitHubReviewState = "COMMENTED"
	ReviewDismissed        GitHubReviewState = "DISMISSED"
)

// GitHubReviewObservation is one submitted or dismissed pull request review, as returned by REST or a webhook.
type GitHubReviewObservation struct {
	ID          int64             `json:"id"`
	Reviewer    GitHubReviewer    `json:"reviewer"`
	State       GitHubReviewState `json:"state"`
	CommitSHA   string            `json:"commitSha"`
	SubmittedAt *time.Time        `json:"submittedAt,omitempty"`
	Body        string            `json:"body"`
	HTMLURL     string            `json:"htmlUrl"`
	Action      *string           `json:"action,omitempty"`
}

// GitHubPRLifecycleObservation is the lifecycle subset that can be recovered from the current PR snapshot.
type GitHubPRLifecycleObservation struct {
	PullRequestNumber int64                  `json:"pullRequestNumber"`
	State             GitHubPullRequestState `json:"state"`
	HeadSHA           string                 `json:"headSha"`
	BaseRef           string                 `json:"baseRef"`
	// Action is the actual webhook action or a lifecycle action inferred from a prior observed snapshot.
	Action *string `json:"action,omitempty"`
}

// NormalizeGitHubReview validates and normalizes one review observation.
func NormalizeGitHubReview(review GitHubReviewObservation) (GitHubReviewObservation, error) {
	if review.ID <= 0 || review.Reviewer.ID <= 0 {
		return GitHubReviewObservation{}, Invalid("review observation must name a review and reviewer above 0")
	}
	if err := CheckText("reviewer login", review.Reviewer.Login, 1, 255); err != nil {
		return GitHubReviewObservation{}, err
	}
	if !IsCommitSHA(review.CommitSHA) {
		return GitHubReviewObservation{}, Invalid("review observation commit SHA is invalid")
	}
	if err := CheckText("review body", review.Body, 0, 65536); err != nil {
		return GitHubReviewObservation{}, err
	}
	if err := CheckText("review HTML URL", review.HTMLURL, 0, 4096); err != nil {
		return GitHubReviewObservation{}, err
	}
	var expectedAction string
	switch review.State {
	case ReviewApproved, ReviewChangesRequested, ReviewCommented:
		expectedAction = "submitted"
	case ReviewDismissed:
		expectedAction = "dismissed"
	default:
		return GitHubReviewObservation{}, Invalid("review observation state is invalid")
	}
	if review.Action == nil {
		review.Action = &expectedAction
	} else if *review.Action != "submitted" && *review.Action != "dismissed" && *review.Action != "edited" {
		return GitHubReviewObservation{}, Invalid("review observation action is invalid")
	}
	if review.SubmittedAt != nil {
		value := review.SubmittedAt.UTC().Truncate(time.Microsecond)
		review.SubmittedAt = &value
	}
	return review, nil
}

// NormalizeGitHubPRLifecycle validates one lifecycle observation recovered from the current PR snapshot.
func NormalizeGitHubPRLifecycle(observation GitHubPRLifecycleObservation) (GitHubPRLifecycleObservation, error) {
	if observation.PullRequestNumber <= 0 || !IsCommitSHA(observation.HeadSHA) {
		return GitHubPRLifecycleObservation{}, Invalid("pull request lifecycle must name a positive PR and commit SHA")
	}
	switch observation.State {
	case GitHubPullRequestOpen, GitHubPullRequestClosed, GitHubPullRequestMerged:
	default:
		return GitHubPRLifecycleObservation{}, Invalid("pull request lifecycle state is invalid")
	}
	if err := CheckText("pull request base ref", observation.BaseRef, 1, 255); err != nil {
		return GitHubPRLifecycleObservation{}, err
	}
	if observation.Action != nil && !actionPattern.MatchString(*observation.Action) {
		return GitHubPRLifecycleObservation{}, Invalid("pull request lifecycle action is invalid")
	}
	return observation, nil
}
