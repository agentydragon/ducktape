package github

import (
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func TestWebhookPRStateNormalizesLowercaseReviewAndKeepsReviewCommit(t *testing.T) {
	body := webhookObject(t, `{
"pull_request":{"number":7,"head":{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}},
"review":{"id":91,"user":{"id":42,"login":"reviewer"},"state":"approved","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","submitted_at":"2026-10-27T10:11:12Z","body":"looks good","html_url":"https://github.test/reviews/91"}
}`)
	action := "submitted"
	review, lifecycle, err := webhookPRState(body, string(model.EventPullRequestReview), &action)
	if err != nil {
		t.Fatal(err)
	}
	if lifecycle != nil {
		t.Fatalf("lifecycle = %+v, want nil", lifecycle)
	}
	if review == nil {
		t.Fatal("review = nil, want a normalized review")
	}
	if review.PullRequestNumber != 7 || review.Review.ID != 91 || review.Review.State != model.ReviewApproved ||
		review.Review.CommitSHA != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ||
		review.Review.Action == nil || *review.Review.Action != "submitted" {
		t.Errorf("review = %+v, want submitted review 91 with its own commit SHA", review)
	}
}

func TestWebhookPRStateNormalizesDismissedReview(t *testing.T) {
	body := webhookObject(t, `{
"pull_request":{"number":8},
"review":{"id":92,"user":{"id":43,"login":"reviewer"},"state":"dismissed","commit_id":"cccccccccccccccccccccccccccccccccccccccc","body":"dismissed","html_url":"https://github.test/reviews/92"}
}`)
	action := "dismissed"
	review, lifecycle, err := webhookPRState(body, string(model.EventPullRequestReview), &action)
	if err != nil {
		t.Fatal(err)
	}
	if lifecycle != nil || review == nil || review.Review.State != model.ReviewDismissed ||
		review.Review.Action == nil || *review.Review.Action != "dismissed" {
		t.Errorf("review = %+v, lifecycle = %+v; want a dismissed review", review, lifecycle)
	}
}

func TestWebhookPRStateSkipsPendingMinimalAndEditedReviewActivity(t *testing.T) {
	tests := []struct {
		name   string
		action string
		body   string
	}{
		{name: "pending", action: "submitted", body: `{"pull_request":{"number":7},"review":{"id":1,"state":"pending"}}`},
		{name: "id-only review", action: "submitted", body: `{"pull_request":{"number":7},"review":{"id":1}}`},
		{name: "missing review", action: "submitted", body: `{"pull_request":{"number":7}}`},
		{name: "missing review id", action: "dismissed", body: `{"pull_request":{"number":7},"review":{"state":"dismissed"}}`},
		{name: "edited stays raw", action: "edited", body: `{"pull_request":{"number":7},"review":{"id":1,"state":"approved"}}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			action := test.action
			review, lifecycle, err := webhookPRState(webhookObject(t, test.body), string(model.EventPullRequestReview), &action)
			if err != nil || review != nil || lifecycle != nil {
				t.Errorf("webhookPRState() = (%+v, %+v, %v), want no derived state and no error", review, lifecycle, err)
			}
		})
	}
}

func TestWebhookPRStateMapsLifecycleUsingStateAndMerged(t *testing.T) {
	tests := []struct {
		name   string
		action string
		state  string
		merged bool
		want   model.GitHubPullRequestState
	}{
		{name: "open", action: "opened", state: "open", want: model.GitHubPullRequestOpen},
		{name: "reopened", action: "reopened", state: "open", want: model.GitHubPullRequestOpen},
		{name: "synchronize", action: "synchronize", state: "open", want: model.GitHubPullRequestOpen},
		{name: "edited", action: "edited", state: "open", want: model.GitHubPullRequestOpen},
		{name: "closed", action: "closed", state: "closed", want: model.GitHubPullRequestClosed},
		{name: "merged", action: "closed", state: "closed", merged: true, want: model.GitHubPullRequestMerged},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			action := test.action
			body := webhookObject(t, `{"pull_request":{"number":9,"state":"`+test.state+`","merged":`+jsonBool(test.merged)+`,"head":{"sha":"dddddddddddddddddddddddddddddddddddddddd"},"base":{"ref":"main"}}}`)
			review, lifecycle, err := webhookPRState(body, string(model.EventPullRequest), &action)
			if err != nil {
				t.Fatal(err)
			}
			if review != nil || lifecycle == nil || lifecycle.PullRequestNumber != 9 || lifecycle.State != test.want ||
				lifecycle.HeadSHA != "dddddddddddddddddddddddddddddddddddddddd" || lifecycle.BaseRef != "main" {
				t.Errorf("lifecycle = %+v, review = %+v; want lifecycle state %q", lifecycle, review, test.want)
			}
		})
	}
}

func TestWebhookPRStatePreservesIncompleteLifecycleAndRejectsMalformedCompleteFacts(t *testing.T) {
	action := "synchronize"
	minimal := webhookObject(t, `{"pull_request":{"number":10,"state":"open","head":{"sha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},"base":{"ref":"main"}}}`)
	review, lifecycle, err := webhookPRState(minimal, string(model.EventPullRequest), &action)
	if err != nil || review != nil || lifecycle != nil {
		t.Errorf("incomplete lifecycle = (%+v, %+v, %v), want no derived state and no error", review, lifecycle, err)
	}

	for name, raw := range map[string]string{
		"bad merged type": `{"pull_request":{"number":10,"state":"open","merged":"false","head":{"sha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},"base":{"ref":"main"}}}`,
		"bad head SHA":    `{"pull_request":{"number":10,"state":"open","merged":false,"head":{"sha":"not-a-sha"},"base":{"ref":"main"}}}`,
		"bad state":       `{"pull_request":{"number":10,"state":"unknown","merged":false,"head":{"sha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},"base":{"ref":"main"}}}`,
	} {
		t.Run(name, func(t *testing.T) {
			_, _, err := webhookPRState(webhookObject(t, raw), string(model.EventPullRequest), &action)
			if err == nil {
				t.Fatal("webhookPRState() succeeded for malformed complete lifecycle fact")
			}
		})
	}
}

func webhookObject(t *testing.T, raw string) object {
	t.Helper()
	parsed, err := asObject(json.RawMessage(raw), "body")
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func jsonBool(value bool) string {
	if value {
		return "true"
	}
	return "false"
}
