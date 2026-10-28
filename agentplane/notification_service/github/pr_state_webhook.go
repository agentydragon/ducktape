package github

import (
	"encoding/json"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/go-github/v88/github"
)

// webhookPRState turns complete review and pull request webhook facts into the same current-state observations
// used by REST recovery. Incomplete payloads remain raw delivery activity and produce no observation.
func webhookPRState(body object, event string, action *string) (*store.GitHubPRReviewObservation, *model.GitHubPRLifecycleObservation, error) {
	switch event {
	case string(model.EventPullRequestReview):
		if !webhookActionIs(action, "submitted", "dismissed") {
			// In particular, edited deliveries remain in the raw delivery stream without inventing a state transition.
			return nil, nil, nil
		}
		reviewObject, err := body.optionalObject("review", "body")
		if err != nil {
			return nil, nil, err
		}
		if reviewObject == nil || reviewObject.present("id") == nil || reviewObject.present("state") == nil ||
			reviewObject.present("user") == nil || reviewObject.present("commit_id") == nil {
			return nil, nil, nil
		}
		pullRequestNumber, found, err := webhookPullRequestNumber(body)
		if err != nil || !found {
			return nil, nil, err
		}
		var review github.PullRequestReview
		if err := json.Unmarshal(body["review"], &review); err != nil {
			return nil, nil, err
		}
		observation, err := client.ReviewObservation(&review)
		if err != nil || observation == nil {
			return nil, nil, err
		}
		observation.Action = action
		return &store.GitHubPRReviewObservation{
			PullRequestNumber: pullRequestNumber,
			Review:            *observation,
		}, nil, nil

	case string(model.EventPullRequest):
		if !webhookActionIs(action, "opened", "closed", "reopened", "synchronize", "edited") {
			return nil, nil, nil
		}
		observation, err := webhookPRLifecycle(body, action)
		return nil, observation, err

	default:
		return nil, nil, nil
	}
}

func webhookPullRequestNumber(body object) (int64, bool, error) {
	pullRequest, err := body.optionalObject("pull_request", "body")
	if err != nil || pullRequest == nil || pullRequest.present("number") == nil {
		return 0, false, err
	}
	number, err := pullRequest.positive("number", "pull_request")
	return number, err == nil, err
}

func webhookPRLifecycle(body object, action *string) (*model.GitHubPRLifecycleObservation, error) {
	pullRequest, err := body.optionalObject("pull_request", "body")
	if err != nil || pullRequest == nil || pullRequest.present("number") == nil {
		return nil, err
	}
	number, err := pullRequest.positive("number", "pull_request")
	if err != nil {
		return nil, err
	}
	head, err := pullRequest.optionalObject("head", "pull_request")
	if err != nil {
		return nil, err
	}
	base, err := pullRequest.optionalObject("base", "pull_request")
	if err != nil {
		return nil, err
	}
	if head == nil || base == nil || pullRequest.present("state") == nil || pullRequest.present("merged") == nil ||
		head.present("sha") == nil || base.present("ref") == nil {
		return nil, nil
	}
	state, err := pullRequest.text("state", "pull_request")
	if err != nil {
		return nil, err
	}
	headSHA, err := head.text("sha", "pull_request.head")
	if err != nil {
		return nil, err
	}
	if !model.IsCommitSHA(headSHA) {
		return nil, invalidPayload("pull_request.head.sha is not a full lowercase Git SHA")
	}
	baseRef, err := base.text("ref", "pull_request.base")
	if err != nil {
		return nil, err
	}
	if utf8.RuneCountInString(baseRef) < 1 || utf8.RuneCountInString(baseRef) > 255 {
		return nil, invalidPayload("pull_request.base.ref must be 1 to 255 characters")
	}
	mergedRaw := pullRequest.present("merged")
	var merged bool
	if err := json.Unmarshal(mergedRaw, &merged); err != nil {
		return nil, invalidPayload("pull_request.merged is not a boolean")
	}
	var lifecycleState model.GitHubPullRequestState
	switch state {
	case "open":
		if merged {
			return nil, invalidPayload("open pull request cannot be merged")
		}
		lifecycleState = model.GitHubPullRequestOpen
	case "closed":
		if merged {
			lifecycleState = model.GitHubPullRequestMerged
		} else {
			lifecycleState = model.GitHubPullRequestClosed
		}
	default:
		return nil, invalidPayload("pull_request.state is invalid")
	}
	return &model.GitHubPRLifecycleObservation{
		PullRequestNumber: number,
		State:             lifecycleState,
		HeadSHA:           headSHA,
		BaseRef:           baseRef,
		Action:            action,
	}, nil
}

func webhookActionIs(action *string, allowed ...string) bool {
	if action == nil {
		return false
	}
	for _, candidate := range allowed {
		if *action == candidate {
			return true
		}
	}
	return false
}
