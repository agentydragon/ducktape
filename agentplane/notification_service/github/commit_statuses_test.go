package github

import (
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

func TestCommitStatusesPaginatesAndPreservesEveryNativeState(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if !strings.HasSuffix(path, "/status") {
			return false
		}
		if got := r.Method; got != http.MethodGet {
			t.Errorf("commit statuses method = %q, want GET", got)
		}
		if got := r.URL.Query().Get("per_page"); got != "100" {
			t.Errorf("combined status per_page = %q, want 100", got)
		}
		switch r.URL.Query().Get("page") {
		case "1":
			replyPage(w, r, path, "per_page=100", 2, fmt.Sprintf(`{"state":"failure","sha":%q,"total_count":4,"statuses":[
{"id":502,"state":"failure","context":"continuous-integration/failure","target_url":"https://ci.test/failure"},
{"id":501,"state":"error","context":"continuous-integration/error","target_url":"https://ci.test/error","updated_at":"2026-10-27T01:06:07-07:00"}]}`, testSHA))
		case "2":
			replyPage(w, r, path, "per_page=100", 0, fmt.Sprintf(`{"state":"failure","sha":%q,"total_count":4,"statuses":[
{"id":504,"state":"success","context":"continuous-integration/success","target_url":"https://ci.test/success","updated_at":"2026-10-27T01:09:10Z"},
{"id":503,"state":"pending","context":"continuous-integration/pending"}]}`, testSHA))
		default:
			t.Errorf("unexpected combined status page %q", r.URL.Query().Get("page"))
			return false
		}
		return true
	}

	statuses, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	want := []store.GitHubCommitStatus{
		{SHA: testSHA, ID: 501, State: store.CommitStatusError, Context: "continuous-integration/error", TargetURL: "https://ci.test/error", UpdatedAt: ciUTC(2026, 10, 27, 8, 6, 7)},
		{SHA: testSHA, ID: 502, State: store.CommitStatusFailure, Context: "continuous-integration/failure", TargetURL: "https://ci.test/failure"},
		{SHA: testSHA, ID: 503, State: store.CommitStatusPending, Context: "continuous-integration/pending"},
		{SHA: testSHA, ID: 504, State: store.CommitStatusSuccess, Context: "continuous-integration/success", TargetURL: "https://ci.test/success", UpdatedAt: ciUTC(2026, 10, 27, 1, 9, 10)},
	}
	if !reflect.DeepEqual(statuses, want) {
		t.Errorf("CommitStatuses() = %+v, want %+v", statuses, want)
	}
	for i, status := range statuses {
		if status.RepositoryID != 0 {
			t.Errorf("status %d repository ID = %d, want provider to fill it", i, status.RepositoryID)
		}
	}
	if fake.count("/repos/owner/repo/commits/"+testSHA+"/status") != 2 {
		t.Errorf("commit status page count = %d, want 2", fake.count("/repos/owner/repo/commits/"+testSHA+"/status"))
	}
	if fake.count("/repos/owner/repo/commits/"+testSHA+"/check-runs") != 0 {
		t.Error("CommitStatuses called the Check Runs endpoint")
	}
}

func TestCommitStatusesUsesStatusesOnlyTokenAndReturnsNonNilEmptySuccess(t *testing.T) {
	fake := newFake(t)
	statuses, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if statuses == nil || len(statuses) != 0 {
		t.Errorf("CommitStatuses() = %#v, want non-nil empty slice", statuses)
	}
	if got := fake.count(tokenPath); got != 1 {
		t.Errorf("installation token requests = %d, want one Commit-statuses-only token", got)
	}
	if fake.count("/repos/owner/repo/commits/"+testSHA+"/check-runs") != 0 {
		t.Error("CommitStatuses called the Check Runs endpoint")
	}
}

func TestCommitStatusesRejectsWrongCommitAndInvalidFacts(t *testing.T) {
	t.Run("different SHA", func(t *testing.T) {
		fake := newFake(t)
		fake.respond("/repos/owner/repo/commits/"+testSHA+"/status", http.StatusOK, nil,
			fmt.Sprintf(`{"state":"success","sha":%q,"total_count":0,"statuses":[]}`, otherBaseSHA))
		_, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
		asError[*SourceChangedError](t, err)
	})
	for name, status := range map[string]string{
		"missing id":      `{"id":0,"state":"success","context":"ci"}`,
		"missing context": `{"id":1,"state":"success"}`,
		"unknown state":   `{"id":1,"state":"unknown","context":"ci"}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := newFake(t)
			fake.respond("/repos/owner/repo/commits/"+testSHA+"/status", http.StatusOK, nil,
				fmt.Sprintf(`{"state":"success","sha":%q,"total_count":1,"statuses":[%s]}`, testSHA, status))
			_, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
			asError[*invalidResponseError](t, err)
		})
	}
	for name, combinedState := range map[string]string{"invalid combined state": "error", "unknown combined state": "unknown"} {
		t.Run(name, func(t *testing.T) {
			fake := newFake(t)
			fake.respond("/repos/owner/repo/commits/"+testSHA+"/status", http.StatusOK, nil,
				fmt.Sprintf(`{"state":%q,"sha":%q,"total_count":0,"statuses":[]}`, combinedState, testSHA))
			_, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
			asError[*invalidResponseError](t, err)
		})
	}
}

func TestCommitStatusesRejectsNonImmutableSHAWithoutRequest(t *testing.T) {
	fake := newFake(t)
	_, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", "main", installationID)
	if err == nil || fake.total() != 0 {
		t.Errorf("CommitStatuses with a branch name = %v after %d requests, want argument error without requests", err, fake.total())
	}
}

func TestCommitStatusesReturnsTypedFailureFromLaterPage(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if !strings.HasSuffix(path, "/status") {
			return false
		}
		if r.URL.Query().Get("page") == "1" {
			replyPage(w, r, path, "per_page=100", 2, fmt.Sprintf(`{"state":"pending","sha":%q,"total_count":1,"statuses":[]}`, testSHA))
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, `{"message":"unavailable"}`)
		}
		return true
	}
	statuses, err := newTestClient(t, fake).CommitStatuses(t.Context(), "owner/repo", testSHA, installationID)
	if failure := asError[*StatusError](t, err); failure.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("failure %+v", failure)
	}
	if statuses != nil {
		t.Errorf("CommitStatuses() on error = %#v, want nil", statuses)
	}
}
