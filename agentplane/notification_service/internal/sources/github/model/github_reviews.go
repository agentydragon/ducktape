package model

import (
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// Reviewer is the stable identity of a review's author.
type Reviewer struct {
	ID    int64  `json:"id"`
	Login string `json:"login"`
}

// ReviewState is the review state reported by GitHub after submission or dismissal.
type ReviewState string

const (
	ReviewApproved         ReviewState = "APPROVED"
	ReviewChangesRequested ReviewState = "CHANGES_REQUESTED"
	ReviewCommented        ReviewState = "COMMENTED"
	ReviewDismissed        ReviewState = "DISMISSED"
)

// ReviewObservation is one submitted or dismissed pull request review, as returned by REST or a webhook.
type ReviewObservation struct {
	ID          int64       `json:"id"`
	Reviewer    Reviewer    `json:"reviewer"`
	State       ReviewState `json:"state"`
	CommitSHA   string      `json:"commitSha"`
	SubmittedAt *time.Time  `json:"submittedAt,omitempty"`
	Body        string      `json:"body"`
	HTMLURL     string      `json:"htmlUrl"`
	Action      *string     `json:"action,omitempty"`
}

// PRLifecycleObservation is the lifecycle subset that can be recovered from the current PR snapshot.
type PRLifecycleObservation struct {
	PullRequestNumber int64            `json:"pullRequestNumber"`
	State             PullRequestState `json:"state"`
	HeadSHA           string           `json:"headSha"`
	BaseRef           string           `json:"baseRef"`
	// Action is the actual webhook action or a lifecycle action inferred from a prior observed snapshot.
	Action *string `json:"action,omitempty"`
}

// NormalizeReview validates and normalizes one review observation.
func NormalizeReview(review ReviewObservation) (ReviewObservation, error) {
	if review.ID <= 0 || review.Reviewer.ID <= 0 {
		return ReviewObservation{}, model.Invalid("review observation must name a review and reviewer above 0")
	}
	if err := model.CheckText("reviewer login", review.Reviewer.Login, 1, 255); err != nil {
		return ReviewObservation{}, err
	}
	if !IsCommitSHA(review.CommitSHA) {
		return ReviewObservation{}, model.Invalid("review observation commit SHA is invalid")
	}
	if err := model.CheckText("review body", review.Body, 0, 65536); err != nil {
		return ReviewObservation{}, err
	}
	if err := model.CheckText("review HTML URL", review.HTMLURL, 0, 4096); err != nil {
		return ReviewObservation{}, err
	}
	var expectedAction string
	switch review.State {
	case ReviewApproved, ReviewChangesRequested, ReviewCommented:
		expectedAction = "submitted"
	case ReviewDismissed:
		expectedAction = "dismissed"
	default:
		return ReviewObservation{}, model.Invalid("review observation state is invalid")
	}
	if review.Action == nil {
		review.Action = &expectedAction
	} else if *review.Action != "submitted" && *review.Action != "dismissed" && *review.Action != "edited" {
		return ReviewObservation{}, model.Invalid("review observation action is invalid")
	}
	if review.SubmittedAt != nil {
		value := review.SubmittedAt.UTC().Truncate(time.Microsecond)
		review.SubmittedAt = &value
	}
	return review, nil
}

// NormalizePRLifecycle validates one lifecycle observation recovered from the current PR snapshot.
func NormalizePRLifecycle(observation PRLifecycleObservation) (PRLifecycleObservation, error) {
	if observation.PullRequestNumber <= 0 || !IsCommitSHA(observation.HeadSHA) {
		return PRLifecycleObservation{}, model.Invalid("pull request lifecycle must name a positive PR and commit SHA")
	}
	switch observation.State {
	case PullRequestOpen, PullRequestClosed, PullRequestMerged:
	default:
		return PRLifecycleObservation{}, model.Invalid("pull request lifecycle state is invalid")
	}
	if err := model.CheckText("pull request base ref", observation.BaseRef, 1, 255); err != nil {
		return PRLifecycleObservation{}, err
	}
	if observation.Action != nil && !actionPattern.MatchString(*observation.Action) {
		return PRLifecycleObservation{}, model.Invalid("pull request lifecycle action is invalid")
	}
	return observation, nil
}
