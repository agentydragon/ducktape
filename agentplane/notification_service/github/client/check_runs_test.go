package client

import (
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func TestCheckRunsPaginatesLatestRunsAndPreservesNativeFacts(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if !strings.HasSuffix(path, "/check-runs") {
			return false
		}
		if got := r.Method; got != http.MethodGet {
			t.Errorf("check runs method = %q, want GET", got)
		}
		if got := r.URL.Query().Get("filter"); got != "latest" {
			t.Errorf("check run filter = %q, want latest", got)
		}
		if got := r.URL.Query().Get("per_page"); got != "100" {
			t.Errorf("check run per_page = %q, want 100", got)
		}
		switch r.URL.Query().Get("page") {
		case "1":
			replyPage(w, r, path, "filter=latest&per_page=100", 2, fmt.Sprintf(`{"total_count":7,"check_runs":[
{"id":202,"head_sha":%q,"name":"build","status":"completed","conclusion":"success","details_url":"https://ci.test/build/202","started_at":"2026-10-27T01:02:03-07:00","completed_at":"2026-10-27T01:04:05-07:00","app":{"id":7001,"slug":"github-actions","name":"GitHub Actions"}},
{"id":105,"head_sha":%q,"name":"queue","status":"queued","details_url":"https://ci.test/queue"},
{"id":203,"head_sha":%q,"name":"run","status":"in_progress"},
{"id":204,"head_sha":%q,"name":"wait","status":"waiting"},
{"id":205,"head_sha":%q,"name":"pending","status":"pending"},
{"id":206,"head_sha":%q,"name":"requested","status":"requested"}]}`, fixture.SHA, fixture.SHA, fixture.SHA, fixture.SHA, fixture.SHA, fixture.SHA))
		case "2":
			replyPage(w, r, path, "filter=latest&per_page=100", 0, fmt.Sprintf(`{"total_count":7,"check_runs":[
{"id":303,"head_sha":%q,"name":"build","status":"completed","conclusion":"failure","details_url":"https://ci.test/build/303","app":{"id":7001,"slug":"github-actions","name":"GitHub Actions"}}]}`, fixture.SHA))
		default:
			t.Errorf("unexpected check run page %q", r.URL.Query().Get("page"))
			return false
		}
		return true
	}

	runs, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	want := []model.GitHubCheckRun{
		{HeadSHA: fixture.SHA, ID: 105, Name: "queue", Status: model.CheckRunQueued, DetailsURL: "https://ci.test/queue"},
		{HeadSHA: fixture.SHA, ID: 202, AppID: 7001, Name: "build", Status: model.CheckRunCompleted, Conclusion: stringPointer("success"), DetailsURL: "https://ci.test/build/202", StartedAt: ciUTC(2026, 10, 27, 8, 2, 3), CompletedAt: ciUTC(2026, 10, 27, 8, 4, 5)},
		{HeadSHA: fixture.SHA, ID: 203, Name: "run", Status: model.CheckRunInProgress},
		{HeadSHA: fixture.SHA, ID: 204, Name: "wait", Status: model.CheckRunWaiting},
		{HeadSHA: fixture.SHA, ID: 205, Name: "pending", Status: model.CheckRunPending},
		{HeadSHA: fixture.SHA, ID: 206, Name: "requested", Status: model.CheckRunRequested},
		{HeadSHA: fixture.SHA, ID: 303, AppID: 7001, Name: "build", Status: model.CheckRunCompleted, Conclusion: stringPointer("failure"), DetailsURL: "https://ci.test/build/303"},
	}
	if !reflect.DeepEqual(runs, want) {
		t.Errorf("CheckRuns() = %+v, want %+v", runs, want)
	}
	for i, run := range runs {
		if run.RepositoryID != 0 {
			t.Errorf("run %d repository ID = %d, want provider to fill it", i, run.RepositoryID)
		}
	}
	if fake.Count("/repos/owner/repo/commits/"+fixture.SHA+"/check-runs") != 2 {
		t.Errorf("check run page count = %d, want 2", fake.Count("/repos/owner/repo/commits/"+fixture.SHA+"/check-runs"))
	}
	if fake.Count("/repos/owner/repo/commits/"+fixture.SHA+"/status") != 0 {
		t.Error("CheckRuns called the Commit statuses endpoint")
	}
}

func TestCheckRunsUsesChecksOnlyTokenAndReturnsNonNilEmptySuccess(t *testing.T) {
	fake := fixture.NewFake(t)
	runs, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if runs == nil || len(runs) != 0 {
		t.Errorf("CheckRuns() = %#v, want non-nil empty slice", runs)
	}
	if got := fake.Count(fixture.TokenPath); got != 1 {
		t.Errorf("installation token requests = %d, want one Checks-only token", got)
	}
	if fake.Count("/repos/owner/repo/commits/"+fixture.SHA+"/status") != 0 {
		t.Error("CheckRuns called the Commit statuses endpoint")
	}
}

func TestCheckRunsRejectsWrongCommitAndInvalidFacts(t *testing.T) {
	t.Run("different SHA", func(t *testing.T) {
		fake := fixture.NewFake(t)
		fake.Respond("/repos/owner/repo/commits/"+fixture.SHA+"/check-runs", http.StatusOK, nil,
			fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":9,"head_sha":%q,"name":"build","status":"completed","conclusion":"success"}]}`, fixture.OtherBaseSHA))
		_, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
		fixture.AsError[*SourceChangedError](t, err)
	})
	for name, body := range map[string]string{
		"missing id":              fmt.Sprintf(`{"id":0,"head_sha":%q,"name":"build","status":"completed","conclusion":"success"}`, fixture.SHA),
		"missing head SHA":        `{"id":1,"name":"build","status":"queued"}`,
		"missing name":            fmt.Sprintf(`{"id":1,"head_sha":%q,"status":"completed","conclusion":"success"}`, fixture.SHA),
		"negative App ID":         fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"queued","app":{"id":-1}}`, fixture.SHA),
		"unknown status":          fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"waiting_for_runner"}`, fixture.SHA),
		"unknown conclusion":      fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"completed","conclusion":"still_running"}`, fixture.SHA),
		"missing conclusion":      fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"completed"}`, fixture.SHA),
		"conclusion while active": fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"in_progress","conclusion":"success"}`, fixture.SHA),
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/commits/"+fixture.SHA+"/check-runs", http.StatusOK, nil,
				`{"total_count":1,"check_runs":[`+body+`]}`)
			_, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
			fixture.AsError[*InvalidResponseError](t, err)
		})
	}
}

func TestCheckRunsRejectsNonImmutableSHAWithoutRequest(t *testing.T) {
	fake := fixture.NewFake(t)
	_, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", "main", installationID)
	if err == nil || fake.Total() != 0 {
		t.Errorf("CheckRuns with a branch name = %v after %d requests, want argument error without requests", err, fake.Total())
	}
}

func TestCheckRunsReturnsTypedFailureFromLaterPage(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		if !strings.HasSuffix(path, "/check-runs") {
			return false
		}
		if r.URL.Query().Get("page") == "1" {
			replyPage(w, r, path, "filter=latest&per_page=100", 2, `{"total_count":1,"check_runs":[]}`)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, `{"message":"unavailable"}`)
		}
		return true
	}

	runs, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
	if failure := fixture.AsError[*StatusError](t, err); failure.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("failure %+v", failure)
	}
	if runs != nil {
		t.Errorf("CheckRuns() on error = %#v, want nil", runs)
	}
}

func TestCheckRunsConclusionsRemainTypedAndExact(t *testing.T) {
	for _, value := range []string{"success", "failure", "neutral", "cancelled", "timed_out", "action_required", "stale", "skipped", "startup_failure"} {
		t.Run(value, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/commits/"+fixture.SHA+"/check-runs", http.StatusOK, nil,
				fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":1,"head_sha":%q,"name":"test","status":"completed","conclusion":%q}]}`, fixture.SHA, value))
			runs, err := newTestClient(t, fake).CheckRuns(t.Context(), "owner/repo", fixture.SHA, installationID)
			if err != nil {
				t.Fatal(err)
			}
			if len(runs) != 1 || runs[0].Conclusion == nil || *runs[0].Conclusion != value {
				t.Errorf("CheckRuns() = %+v, want exact conclusion %q", runs, value)
			}
		})
	}
}

func stringPointer(value string) *string { return &value }

func ciUTC(year int, month time.Month, day, hour, minute, second int) *time.Time {
	value := time.Date(year, month, day, hour, minute, second, 0, time.UTC)
	return &value
}
