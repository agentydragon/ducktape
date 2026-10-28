package github

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

func TestCommitCIReadsLatestChecksAndCombinedStatusesAcrossPages(t *testing.T) {
	fake := newFake(t)
	fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
		page := r.URL.Query().Get("page")
		switch {
		case strings.HasSuffix(path, "/check-runs"):
			if got := r.URL.Query().Get("filter"); got != "latest" {
				t.Errorf("check run filter = %q, want latest", got)
			}
			if got := r.URL.Query().Get("per_page"); got != "100" {
				t.Errorf("check run per_page = %q, want 100", got)
			}
			switch page {
			case "1":
				replyCIPage(w, r, path, "filter=latest&per_page=100", 2, fmt.Sprintf(`{"total_count":7,"check_runs":[
{"id":202,"head_sha":%q,"name":"build","status":"completed","conclusion":"success","details_url":"https://ci.test/build/202","started_at":"2026-10-27T01:02:03-07:00","completed_at":"2026-10-27T01:04:05-07:00","app":{"id":7001,"slug":"github-actions","name":"GitHub Actions"}},
{"id":105,"head_sha":%q,"name":"queue","status":"queued","details_url":"https://ci.test/queue"},
{"id":203,"head_sha":%q,"name":"run","status":"in_progress"},
{"id":204,"head_sha":%q,"name":"wait","status":"waiting"},
{"id":205,"head_sha":%q,"name":"pending","status":"pending"},
{"id":206,"head_sha":%q,"name":"requested","status":"requested"}]}`, testSHA, testSHA, testSHA, testSHA, testSHA, testSHA))
			case "2":
				replyCIPage(w, r, path, "filter=latest&per_page=100", 0, fmt.Sprintf(`{"total_count":7,"check_runs":[
{"id":303,"head_sha":%q,"name":"build","status":"completed","conclusion":"failure","details_url":"https://ci.test/build/303","app":{"id":7001,"slug":"github-actions","name":"GitHub Actions"}}]}`, testSHA))
			default:
				t.Errorf("unexpected check run page %q", page)
				return false
			}
			return true
		case strings.HasSuffix(path, "/status"):
			if got := r.URL.Query().Get("per_page"); got != "100" {
				t.Errorf("combined status per_page = %q, want 100", got)
			}
			switch page {
			case "1":
				replyCIPage(w, r, path, "per_page=100", 2, fmt.Sprintf(`{"state":"failure","sha":%q,"total_count":2,"statuses":[
{"id":501,"state":"pending","context":"continuous-integration/legacy","target_url":"https://ci.test/legacy","updated_at":"2026-10-27T01:06:07-07:00"}]}`, testSHA))
			case "2":
				replyCIPage(w, r, path, "per_page=100", 0, fmt.Sprintf(`{"state":"failure","sha":%q,"total_count":2,"statuses":[
{"id":502,"state":"success","context":"security/audit","target_url":"https://ci.test/security"}]}`, testSHA))
			default:
				t.Errorf("unexpected combined status page %q", page)
				return false
			}
			return true
		default:
			return false
		}
	}

	client := newTestClient(t, fake)
	observations, err := client.CommitCI(t.Context(), "owner/repo", testSHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	wantEvents := []store.GitHubCIObservation{
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("completed"), CheckAppID: 7001, ID: 202, Name: "build", Status: "completed", Conclusion: conclusion("success"), DetailsURL: "https://ci.test/build/202", StartedAt: utcTime(2026, 10, 27, 8, 2, 3), CompletedAt: utcTime(2026, 10, 27, 8, 4, 5)},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("completed"), CheckAppID: 7001, ID: 303, Name: "build", Status: "completed", Conclusion: conclusion("failure"), DetailsURL: "https://ci.test/build/303"},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("created"), ID: 205, Name: "pending", Status: "pending"},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("created"), ID: 105, Name: "queue", Status: "queued", DetailsURL: "https://ci.test/queue"},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("created"), ID: 206, Name: "requested", Status: "requested"},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("in_progress"), ID: 203, Name: "run", Status: "in_progress"},
		{HeadSHA: testSHA, Event: store.EventCheckRun, Action: action("created"), ID: 204, Name: "wait", Status: "waiting"},
		{HeadSHA: testSHA, Event: store.EventStatus, ID: 501, Name: "continuous-integration/legacy", Status: "pending", DetailsURL: "https://ci.test/legacy", UpdatedAt: utcTime(2026, 10, 27, 8, 6, 7)},
		{HeadSHA: testSHA, Event: store.EventStatus, ID: 502, Name: "security/audit", Status: "success", DetailsURL: "https://ci.test/security"},
	}
	if len(observations) != len(wantEvents) {
		t.Fatalf("CommitCI returned %d observations, want %d: %+v", len(observations), len(wantEvents), observations)
	}
	for i := range wantEvents {
		if !reflect.DeepEqual(observations[i], wantEvents[i]) {
			t.Errorf("observation %d = %+v, want %+v", i, observations[i], wantEvents[i])
		}
		if observations[i].RepositoryID != 0 {
			t.Errorf("observation %d repository ID = %d, want provider to fill it", i, observations[i].RepositoryID)
		}
	}
	if fake.count("/repos/owner/repo/commits/"+testSHA+"/check-runs") != 2 ||
		fake.count("/repos/owner/repo/commits/"+testSHA+"/status") != 2 {
		t.Errorf("CI endpoint page counts: check runs=%d, combined status=%d", fake.count("/repos/owner/repo/commits/"+testSHA+"/check-runs"), fake.count("/repos/owner/repo/commits/"+testSHA+"/status"))
	}
}

func TestWebhookCheckRunObservationPreservesCheckAppID(t *testing.T) {
	var body object
	if err := json.Unmarshal([]byte(fmt.Sprintf(`{"check_run":{"id":42,"head_sha":%q,"name":"build","status":"completed","conclusion":"success","app":{"id":812,"slug":"ci","name":"CI"}}}`, testSHA)), &body); err != nil {
		t.Fatal(err)
	}
	observation, err := webhookCI(body, string(store.EventCheckRun), action("completed"), testSHA)
	if err != nil {
		t.Fatal(err)
	}
	if observation == nil || observation.CheckAppID != 812 {
		t.Fatalf("webhook observation = %+v, want check app 812", observation)
	}
}

func TestCommitCIUsesSeparateCIOnlyInstallationTokenAndEmptyDefaults(t *testing.T) {
	fake := newFake(t)
	client := newTestClient(t, fake)
	if _, err := client.Repository(t.Context(), "owner/repo", installationID); err != nil {
		t.Fatal(err)
	}
	observations, err := client.CommitCI(t.Context(), "owner/repo", testSHA, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if len(observations) != 0 {
		t.Errorf("empty CI endpoints returned %+v", observations)
	}
	if got := fake.count(tokenPath); got != 2 {
		t.Errorf("installation token requests = %d, want separate core and CI tokens", got)
	}
}

func TestCommitCIRejectsWrongCommitAndInvalidCIFields(t *testing.T) {
	t.Run("check run from a different SHA", func(t *testing.T) {
		fake := newFake(t)
		fake.respond("/repos/owner/repo/commits/"+testSHA+"/check-runs", http.StatusOK, nil,
			fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":9,"head_sha":%q,"name":"build","status":"completed","conclusion":"success"}]}`, otherBaseSHA))
		_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", testSHA, installationID)
		asError[*SourceChangedError](t, err)
		if fake.count("/repos/owner/repo/commits/"+testSHA+"/status") != 0 {
			t.Error("status endpoint was called after a mismatched check run SHA")
		}
	})
	t.Run("combined status from a different SHA", func(t *testing.T) {
		fake := newFake(t)
		fake.respond("/repos/owner/repo/commits/"+testSHA+"/status", http.StatusOK, nil,
			fmt.Sprintf(`{"state":"success","sha":%q,"total_count":0,"statuses":[]}`, otherBaseSHA))
		_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", testSHA, installationID)
		asError[*SourceChangedError](t, err)
	})
	for name, body := range map[string]string{
		"missing id":     fmt.Sprintf(`{"id":0,"head_sha":%q,"name":"build","status":"completed","conclusion":"success"}`, testSHA),
		"missing name":   fmt.Sprintf(`{"id":1,"head_sha":%q,"status":"completed","conclusion":"success"}`, testSHA),
		"unknown status": fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"waiting_for_runner"}`, testSHA),
		"unknown result": fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"completed","conclusion":"still_running"}`, testSHA),
		"missing result": fmt.Sprintf(`{"id":1,"head_sha":%q,"name":"build","status":"completed"}`, testSHA),
	} {
		t.Run(name, func(t *testing.T) {
			fake := newFake(t)
			fake.respond("/repos/owner/repo/commits/"+testSHA+"/check-runs", http.StatusOK, nil,
				`{"total_count":1,"check_runs":[`+body+`]}`)
			_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", testSHA, installationID)
			asError[*invalidResponseError](t, err)
		})
	}
	for name, status := range map[string]string{
		"missing id":      `{"id":0,"state":"success","context":"ci"}`,
		"missing context": `{"id":1,"state":"success"}`,
		"unknown state":   `{"id":1,"state":"unknown","context":"ci"}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := newFake(t)
			fake.respond("/repos/owner/repo/commits/"+testSHA+"/status", http.StatusOK, nil,
				fmt.Sprintf(`{"state":"success","sha":%q,"total_count":1,"statuses":[%s]}`, testSHA, status))
			_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", testSHA, installationID)
			asError[*invalidResponseError](t, err)
		})
	}
}

func TestCommitCIRejectsNonImmutableSHA(t *testing.T) {
	fake := newFake(t)
	_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", "main", installationID)
	if err == nil || fake.total() != 0 {
		t.Errorf("CommitCI with a branch name = %v after %d requests, want argument error without requests", err, fake.total())
	}
}

func TestCommitCIReturnsTypedFailuresFromLaterPages(t *testing.T) {
	t.Run("HTTP failure", func(t *testing.T) {
		fake := newFake(t)
		fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
			if !strings.HasSuffix(path, "/check-runs") {
				return false
			}
			if r.URL.Query().Get("page") == "1" {
				replyCIPage(w, r, path, "filter=latest&per_page=100", 2, `{"total_count":1,"check_runs":[]}`)
			} else {
				w.WriteHeader(http.StatusServiceUnavailable)
				fmt.Fprint(w, `{"message":"unavailable"}`)
			}
			return true
		}
		_, err := newTestClient(t, fake).CommitCI(t.Context(), "owner/repo", testSHA, installationID)
		if failure := asError[*StatusError](t, err); failure.StatusCode != http.StatusServiceUnavailable {
			t.Errorf("failure %+v", failure)
		}
	})
	t.Run("request timeout", func(t *testing.T) {
		fake := newFake(t)
		fake.handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
			if !strings.HasSuffix(path, "/check-runs") {
				return false
			}
			if r.URL.Query().Get("page") == "1" {
				replyCIPage(w, r, path, "filter=latest&per_page=100", 2, `{"total_count":1,"check_runs":[]}`)
				return true
			}
			<-r.Context().Done()
			return true
		}
		client := newTestClient(t, fake, func(s *Settings) { s.RequestTimeoutSeconds = 0.2 })
		_, err := client.CommitCI(t.Context(), "owner/repo", testSHA, installationID)
		if transport := asError[*TransportError](t, err); transport == nil || transport.Err == nil {
			t.Errorf("failure %v", err)
		}
	})
}

func replyCIPage(w http.ResponseWriter, r *http.Request, path, query string, nextPage int, body string) {
	if nextPage != 0 {
		w.Header().Set("Link", fmt.Sprintf(`<http://%s%s?%s&page=%d>; rel="next"`, r.Host, path, query, nextPage))
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, body)
}

func action(value string) *string { return &value }

func conclusion(value string) *string { return &value }

func utcTime(year int, month time.Month, day, hour, minute, second int) *time.Time {
	value := time.Date(year, month, day, hour, minute, second, 0, time.UTC)
	return &value
}
