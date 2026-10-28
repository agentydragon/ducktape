package github

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/go-github/v88/github"
)

func TestPullRequestReviewsPaginatesAndPreservesReviewFacts(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if path != "/repos/owner/repo/pulls/7/reviews" {
			return false
		}
		if r.Method != http.MethodGet {
			t.Errorf("review method = %q, want GET", r.Method)
		}
		if got := r.URL.Query().Get("per_page"); got != "100" {
			t.Errorf("review per_page = %q, want 100", got)
		}
		switch r.URL.Query().Get("page") {
		case "1":
			replyReviewPage(w, r, 2, fmt.Sprintf(`[
{"id":31,"user":{"id":101,"login":"alice"},"state":"APPROVED","commit_id":%q,"submitted_at":"2026-10-27T01:02:03-07:00","body":"approved old head","html_url":"https://github.test/reviews/31"},
{"id":32,"user":{"id":102,"login":"bob"},"state":"CHANGES_REQUESTED","commit_id":%q,"submitted_at":"2026-10-27T01:04:05Z","body":"please revise","html_url":"https://github.test/reviews/32"},
{"id":33,"user":{"id":103,"login":"carol"},"state":"COMMENTED","commit_id":%q,"submitted_at":"2026-10-27T01:05:06Z","body":"one thought","html_url":"https://github.test/reviews/33"},
{"id":34,"user":{"id":104,"login":"draft-reviewer"},"state":"PENDING","commit_id":%q,"body":"not submitted"}]`, testBaseSHA, otherBaseSHA, testSHA, testSHA))
		case "2":
			replyReviewPage(w, r, 0, fmt.Sprintf(`[
{"id":35,"user":{"id":105,"login":"dana"},"state":"DISMISSED","commit_id":%q,"submitted_at":"2026-10-27T01:06:07Z","body":"dismissed","html_url":"https://github.test/reviews/35"}]`, otherBaseSHA))
		default:
			t.Errorf("unexpected review page %q", r.URL.Query().Get("page"))
			return false
		}
		return true
	}

	observations, err := newTestClient(t, fake).PullRequestReviews(t.Context(), "owner/repo", 7, installationID)
	if err != nil {
		t.Fatal(err)
	}
	want := []store.GitHubReviewObservation{
		{ID: 31, Reviewer: store.GitHubReviewer{ID: 101, Login: "alice"}, State: store.ReviewApproved, CommitSHA: testBaseSHA, SubmittedAt: reviewUTC(2026, 10, 27, 8, 2, 3), Body: "approved old head", HTMLURL: "https://github.test/reviews/31"},
		{ID: 32, Reviewer: store.GitHubReviewer{ID: 102, Login: "bob"}, State: store.ReviewChangesRequested, CommitSHA: otherBaseSHA, SubmittedAt: reviewUTC(2026, 10, 27, 1, 4, 5), Body: "please revise", HTMLURL: "https://github.test/reviews/32"},
		{ID: 33, Reviewer: store.GitHubReviewer{ID: 103, Login: "carol"}, State: store.ReviewCommented, CommitSHA: testSHA, SubmittedAt: reviewUTC(2026, 10, 27, 1, 5, 6), Body: "one thought", HTMLURL: "https://github.test/reviews/33"},
		{ID: 35, Reviewer: store.GitHubReviewer{ID: 105, Login: "dana"}, State: store.ReviewDismissed, CommitSHA: otherBaseSHA, SubmittedAt: reviewUTC(2026, 10, 27, 1, 6, 7), Body: "dismissed", HTMLURL: "https://github.test/reviews/35"},
	}
	if !reflect.DeepEqual(observations, want) {
		t.Errorf("PullRequestReviews() = %+v, want %+v", observations, want)
	}
	if fake.count("/repos/owner/repo/pulls/7/reviews") != 2 {
		t.Errorf("review page count = %d, want 2", fake.count("/repos/owner/repo/pulls/7/reviews"))
	}
}

func TestPullRequestReviewsKeepsDismissalOnTheSameReviewIdentity(t *testing.T) {
	fake := newFake(t)
	state := "CHANGES_REQUESTED"
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if path != "/repos/owner/repo/pulls/7/reviews" {
			return false
		}
		replyReviewPage(w, r, 0, fmt.Sprintf(`[{"id":91,"user":{"id":401,"login":"reviewer"},"state":%q,"commit_id":%q,"submitted_at":"2026-10-27T01:02:03Z","body":"review","html_url":"https://github.test/reviews/91"}]`, state, testBaseSHA))
		return true
	}

	client := newTestClient(t, fake)
	submitted, err := client.PullRequestReviews(t.Context(), "owner/repo", 7, installationID)
	if err != nil {
		t.Fatal(err)
	}
	state = "DISMISSED"
	dismissed, err := client.PullRequestReviews(t.Context(), "owner/repo", 7, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if len(submitted) != 1 || len(dismissed) != 1 {
		t.Fatalf("review reads returned %d then %d observations, want 1 each", len(submitted), len(dismissed))
	}
	if submitted[0].ID != dismissed[0].ID || submitted[0].State != store.ReviewChangesRequested || dismissed[0].State != store.ReviewDismissed {
		t.Errorf("review transition = (%+v) to (%+v), want stable ID 91 changing state", submitted[0], dismissed[0])
	}
}

func TestReviewObservationSkipsPendingAndRejectsMalformedFacts(t *testing.T) {
	t.Run("pending is not submitted", func(t *testing.T) {
		var review github.PullRequestReview
		if err := json.Unmarshal([]byte(`{"state":"PENDING"}`), &review); err != nil {
			t.Fatal(err)
		}
		observation, err := reviewObservation(&review)
		if err != nil || observation != nil {
			t.Errorf("reviewObservation(pending) = %+v, %v; want nil, nil", observation, err)
		}
	})
	t.Run("lowercase pending is not submitted", func(t *testing.T) {
		var review github.PullRequestReview
		if err := json.Unmarshal([]byte(`{"state":"pending"}`), &review); err != nil {
			t.Fatal(err)
		}
		observation, err := reviewObservation(&review)
		if err != nil || observation != nil {
			t.Errorf("reviewObservation(lowercase pending) = %+v, %v; want nil, nil", observation, err)
		}
	})

	for name, body := range map[string]string{
		"missing ID":             `{"user":{"id":1,"login":"a"},"state":"APPROVED","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
		"missing reviewer":       `{"id":1,"state":"APPROVED","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
		"missing reviewer ID":    `{"id":1,"user":{"login":"a"},"state":"APPROVED","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
		"missing reviewer login": `{"id":1,"user":{"id":1},"state":"APPROVED","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
		"missing commit SHA":     `{"id":1,"user":{"id":1,"login":"a"},"state":"APPROVED"}`,
		"invalid commit SHA":     `{"id":1,"user":{"id":1,"login":"a"},"state":"APPROVED","commit_id":"main"}`,
		"unknown state":          `{"id":1,"user":{"id":1,"login":"a"},"state":"REVIEWED","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
		"mixed-case state":       `{"id":1,"user":{"id":1,"login":"a"},"state":"Approved","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`,
	} {
		t.Run(name, func(t *testing.T) {
			var review github.PullRequestReview
			if err := json.Unmarshal([]byte(body), &review); err != nil {
				t.Fatal(err)
			}
			_, err := reviewObservation(&review)
			asError[*invalidResponseError](t, err)
		})
	}
}

func TestReviewObservationMapsLowercaseWebhookStates(t *testing.T) {
	for rawState, wantState := range map[string]store.GitHubReviewState{
		"approved":          store.ReviewApproved,
		"changes_requested": store.ReviewChangesRequested,
		"commented":         store.ReviewCommented,
		"dismissed":         store.ReviewDismissed,
	} {
		t.Run(rawState, func(t *testing.T) {
			var review github.PullRequestReview
			body := fmt.Sprintf(`{"id":1,"user":{"id":2,"login":"reviewer"},"state":%q,"commit_id":%q}`, rawState, testBaseSHA)
			if err := json.Unmarshal([]byte(body), &review); err != nil {
				t.Fatal(err)
			}
			observation, err := reviewObservation(&review)
			if err != nil {
				t.Fatal(err)
			}
			if observation == nil || observation.State != wantState {
				t.Errorf("reviewObservation(%q) = %+v, want state %q", rawState, observation, wantState)
			}
		})
	}
}

func TestPullRequestReviewsReturnsNonNilEmptySuccess(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if path != "/repos/owner/repo/pulls/7/reviews" {
			return false
		}
		replyReviewPage(w, r, 0, `[]`)
		return true
	}

	observations, err := newTestClient(t, fake).PullRequestReviews(t.Context(), "owner/repo", 7, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if observations == nil || len(observations) != 0 {
		t.Errorf("PullRequestReviews() = %#v, want non-nil empty slice", observations)
	}
}

func TestPullRequestReviewsRejectsInvalidNumberWithoutRequest(t *testing.T) {
	fake := newFake(t)
	for _, number := range []int{0, -1} {
		if _, err := newTestClient(t, fake).PullRequestReviews(t.Context(), "owner/repo", number, installationID); err == nil {
			t.Errorf("PullRequestReviews(number=%d) succeeded", number)
		}
	}
	if fake.total() != 0 {
		t.Errorf("invalid numbers triggered %d GitHub requests", fake.total())
	}
}

func TestPullRequestReviewsPreservesTypedFailureFromLaterPage(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if path != "/repos/owner/repo/pulls/7/reviews" {
			return false
		}
		if r.URL.Query().Get("page") == "1" {
			replyReviewPage(w, r, 2, `[]`)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, `{"message":"unavailable"}`)
		}
		return true
	}

	observations, err := newTestClient(t, fake).PullRequestReviews(t.Context(), "owner/repo", 7, installationID)
	if failure := asError[*StatusError](t, err); failure.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("failure = %+v", failure)
	}
	if observations != nil {
		t.Errorf("PullRequestReviews() on error = %#v, want nil", observations)
	}
}

func replyReviewPage(w http.ResponseWriter, r *http.Request, nextPage int, body string) {
	if nextPage != 0 {
		w.Header().Set("Link", fmt.Sprintf(`<http://%s%s?per_page=%d&page=%d>; rel="next"`, r.Host, r.URL.Path, reviewPageSize, nextPage))
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, body)
}

func reviewUTC(year int, month time.Month, day, hour, minute, second int) *time.Time {
	value := time.Date(year, month, day, hour, minute, second, 0, time.UTC)
	return &value
}
