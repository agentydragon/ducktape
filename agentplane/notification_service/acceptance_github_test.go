package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

const (
	acceptanceRepositoryID = 100
	acceptanceInstallation = 11
	acceptancePullRequest  = 7
	acceptanceWebhookKey   = "test-webhook-secret!"
	acceptanceHeadSHA      = fixture.SHA
	acceptanceBaseSHA      = fixture.BaseSHA
)

const (
	acceptancePRPath       = "/repos/owner/repo/pulls/7"
	acceptanceReviewsPath  = "/repos/owner/repo/pulls/7/reviews"
	acceptanceChecksPath   = "/repos/owner/repo/commits/" + fixture.SHA + "/check-runs"
	acceptanceStatusesPath = "/repos/owner/repo/commits/" + fixture.SHA + "/status"
)

func acceptanceOpenPullRequest() string {
	return fmt.Sprintf(`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":%q},"mergeable":null,"head":{"sha":%q,"repo":{"id":100,"full_name":"owner/repo"}}}`,
		acceptanceBaseSHA, acceptanceHeadSHA)
}

func acceptanceMergedPullRequest() string {
	return fmt.Sprintf(`{"number":7,"state":"closed","merged":true,"base":{"ref":"main","sha":%q},"mergeable":null,"head":{"sha":%q,"repo":{"id":100,"full_name":"owner/repo"}}}`,
		acceptanceBaseSHA, acceptanceHeadSHA)
}

func acceptanceCheckRuns() string {
	return fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":31,"name":"build","head_sha":%q,"status":"completed","conclusion":"success","details_url":"https://github.example.test/checks/31","started_at":"2026-10-27T00:00:00Z","completed_at":"2026-10-27T00:01:00Z"}]}`,
		acceptanceHeadSHA)
}

const acceptanceCommitStatuses = `{"sha":"` + fixture.SHA + `","state":"success","total_count":1,"statuses":[{"id":41,"context":"build/status","state":"success","target_url":"https://github.example.test/status","updated_at":"2026-10-27T00:01:00Z"}]}`

const acceptanceReviews = `[{"id":51,"user":{"id":101,"login":"reviewer"},"state":"APPROVED","commit_id":"` + fixture.SHA + `","submitted_at":"2026-10-27T00:02:00Z","body":"Looks good","html_url":"https://github.example.test/reviews/51"}]`

func clearGitHubAcceptanceState(t *testing.T) {
	t.Helper()
	// The service integration suite is serial. Keep its reused fixture IDs and shared observations isolated.
	if _, err := testDatabase(t).ExecContext(t.Context(), `TRUNCATE github_delivery, github_installation, github_repository CASCADE`); err != nil {
		t.Fatal(err)
	}
}

func postSignedGitHubDelivery(t *testing.T, service running, event string, payload any) {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	mac := hmac.New(sha256.New, []byte(acceptanceWebhookKey))
	_, _ = mac.Write(body)
	request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, service.url+"/v1/webhooks/github", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("X-GitHub-Event", event)
	request.Header.Set("X-GitHub-Delivery", uuid.NewString())
	request.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	response, err := (&http.Client{Timeout: callDeadline}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusAccepted {
		t.Fatalf("signed %s webhook = %d, want 202", event, response.StatusCode)
	}
}

func githubSubscription(t *testing.T, service running, id string) map[string]any {
	t.Helper()
	code, result := agentCall(t, service, http.MethodGet, "/v1/subscriptions/"+id, sandboxToken, nil)
	if code != http.StatusOK {
		t.Fatalf("get GitHub subscription = %d %v", code, result)
	}
	return at(t, result, "subscription").(map[string]any)
}

func inboxEntries(t *testing.T, service running, inboxID string) []any {
	t.Helper()
	code, page := agentCall(t, service, http.MethodGet,
		"/v1/inboxes/"+inboxID+"/entries?limit=128&after_cursor=0", sandboxToken, nil)
	if code != http.StatusOK {
		t.Fatalf("read recovered inbox = %d %v", code, page)
	}
	return at(t, page, "entries").([]any)
}

func hasGitHubBaseline(subscription map[string]any) bool {
	github, ok := subscription["github"].(map[string]any)
	if !ok {
		return false
	}
	for _, field := range []string{"subject", "checkRuns", "commitStatuses", "reviews"} {
		status, ok := github[field].(map[string]any)
		if !ok || status["lastSuccessAt"] == nil {
			return false
		}
	}
	return true
}

func advanceGitHubAcceptanceDeadlines(t *testing.T, service running, subscriptionID, inboxID string) {
	t.Helper()
	for _, statement := range []struct {
		query string
		args  []any
	}{
		{`UPDATE github_repository_access SET valid_until = now() - interval '1 second', next_attempt = NULL, claim = NULL, claim_until = NULL WHERE app_id = 42 AND repository_id = 100`, nil},
		{`UPDATE github_subject SET last_success_at = now() - interval '11 minutes', next_attempt = NULL, claim = NULL, claim_until = NULL WHERE repository_id = 100 AND kind = 'pull_request' AND subject_key = '7'`, nil},
		{`UPDATE github_commit_refresh SET last_success_at = now() - interval '11 minutes', next_attempt = NULL, claim = NULL, claim_until = NULL WHERE app_id = 42 AND repository_id = 100 AND subject_kind = 'pull_request' AND subject_key = '7'`, nil},
		{`UPDATE github_review_refresh SET last_success_at = now() - interval '11 minutes', next_attempt = NULL, claim = NULL, claim_until = NULL WHERE app_id = 42 AND repository_id = 100 AND subject_kind = 'pull_request' AND subject_key = '7'`, nil},
		{`UPDATE subscription SET next_attempt = now() WHERE id = $1`, []any{subscriptionID}},
		{`UPDATE inbox SET next_attempt = now(), claim = NULL WHERE id = $1`, []any{inboxID}},
	} {
		result, err := service.db.ExecContext(t.Context(), statement.query, statement.args...)
		if err != nil {
			t.Fatal(err)
		}
		rows, err := result.RowsAffected()
		if err != nil {
			t.Fatal(err)
		}
		if rows == 0 {
			t.Fatalf("deadline update affected no rows: %s", statement.query)
		}
	}
}

func recoveredGitHubFactSet(t *testing.T, entries []any, subscriptionID string) map[string]map[string]any {
	t.Helper()
	facts := make(map[string]map[string]any, len(entries))
	for _, raw := range entries {
		entry := raw.(map[string]any)
		identity := at(t, entry, "event", "githubObservation").(map[string]any)
		name := identity["event"].(string)
		if _, exists := facts[name]; exists {
			t.Fatalf("duplicate %s observation in inbox: %v", name, entries)
		}
		sequence, ok := identity["sequence"].(string)
		parsedSequence, err := strconv.ParseInt(sequence, 10, 64)
		if !ok || err != nil || parsedSequence <= 0 || identity["appId"] != "42" || identity["repositoryId"] != "100" {
			t.Errorf("observation identity = %v", identity)
		}
		matched := false
		for _, rawID := range entry["subscriptionIds"].([]any) {
			if rawID == subscriptionID {
				matched = true
				break
			}
		}
		if !matched {
			t.Errorf("observation %s did not name subscription %s: %v", name, subscriptionID, entry["subscriptionIds"])
		}
		facts[name] = entry["payload"].(map[string]any)
	}
	return facts
}

func TestGitHubCurrentFactsRecoverThroughServiceAfterRestartAndDeduplicateLateWebhooks(t *testing.T) {
	clearGitHubAcceptanceState(t)
	api := fixture.NewFake(t)
	api.Respond(acceptancePRPath, http.StatusOK, nil, acceptanceOpenPullRequest())
	apiServer := httptest.NewServer(api)
	t.Cleanup(apiServer.Close)

	dispatcher, history := &losingDispatcher{}, &journal{}
	historyAddress := serveGRPC(t, func(server *grpc.Server) { historypb.RegisterHistoryServiceServer(server, history) })
	dispatcherAddress := serveGRPC(t, func(server *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(server, dispatcher) })
	tune := func(c *config) {
		c.GitHub = fixture.Settings(apiServer.URL)
		c.GitHub.WebhookSecret = acceptanceWebhookKey
		c.HistoryService.Target, c.SessionDispatcher.Target = historyAddress, dispatcherAddress
		c.NoticeDebounce.QuietSeconds = 0
	}
	service := start(t, tune)

	code, created := agentCall(t, service, http.MethodPost, "/v1/subscriptions", sandboxToken, map[string]any{
		"destinationRef": map[string]any{"namespace": testNamespace, "name": sandboxName, "uid": sandboxUID},
		"sessionId":      testSession,
		"idempotencyKey": "missed-github-updates",
		"source": map[string]any{"github": map[string]any{
			"repository": "owner/repo", "subject": map[string]any{"pullRequest": map[string]any{"number": acceptancePullRequest}},
		}},
	})
	if code != http.StatusOK {
		t.Fatalf("subscribe to GitHub pull request = %d %v", code, created)
	}
	subscription := at(t, created, "subscription").(map[string]any)
	subscriptionID, inboxID := subscription["id"].(string), subscription["inboxId"].(string)

	// The OPEN snapshot and empty GitHub collections establish a quiet baseline without webhook history.
	eventually(t, "an empty GitHub baseline", func() bool {
		return hasGitHubBaseline(githubSubscription(t, service, subscriptionID))
	})
	if entries := inboxEntries(t, service, inboxID); len(entries) != 0 {
		t.Fatalf("baseline entries = %v, want a quiet OPEN state with no facts", entries)
	}
	var receipts int
	if err := service.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_delivery WHERE app_id = 42`).Scan(&receipts); err != nil {
		t.Fatal(err)
	}
	if receipts != 0 {
		t.Fatalf("baseline has %d webhook receipts, want none", receipts)
	}
	for _, path := range []string{acceptancePRPath, acceptanceChecksPath, acceptanceStatusesPath, acceptanceReviewsPath} {
		if count := api.Count(path); count == 0 {
			t.Errorf("baseline did not read %s", path)
		}
	}

	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Fatalf("the baseline service stopped with %v, want cancellation", err)
	}
	api.Respond(acceptancePRPath, http.StatusOK, nil, acceptanceMergedPullRequest())
	api.Respond(acceptanceChecksPath, http.StatusOK, nil, acceptanceCheckRuns())
	api.Respond(acceptanceStatusesPath, http.StatusOK, nil, acceptanceCommitStatuses)
	api.Respond(acceptanceReviewsPath, http.StatusOK, nil, acceptanceReviews)
	advanceGitHubAcceptanceDeadlines(t, service, subscriptionID, inboxID)

	// Startup alone must discover the durable deadlines; no webhook, database notification or manual worker wake is sent.
	service = restart(t, tune)
	var recovered []any
	eventually(t, "four current GitHub facts repaired by the restarted service", func() bool {
		recovered = inboxEntries(t, service, inboxID)
		return len(recovered) == 4
	})
	var recoveredReceipts int
	if err := service.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_delivery WHERE app_id = 42`).Scan(&recoveredReceipts); err != nil {
		t.Fatal(err)
	}
	if recoveredReceipts != 0 {
		t.Fatalf("recovery used %d webhook receipts, want polling without a webhook", recoveredReceipts)
	}
	facts := recoveredGitHubFactSet(t, recovered, subscriptionID)
	checkRun, checkPresent := facts["GITHUB_EVENT_NAME_CHECK_RUN"]
	status, statusPresent := facts["GITHUB_EVENT_NAME_STATUS"]
	review, reviewPresent := facts["GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW"]
	lifecycle, lifecyclePresent := facts["GITHUB_EVENT_NAME_PULL_REQUEST"]
	if len(facts) != 4 || !checkPresent || !statusPresent || !reviewPresent || !lifecyclePresent {
		t.Fatalf("recovered fact categories = %v, want check-run, status, review and lifecycle", facts)
	}
	if checkRun["id"] != float64(31) || checkRun["status"] != "completed" || checkRun["conclusion"] != "success" || checkRun["headSha"] != acceptanceHeadSHA {
		t.Errorf("recovered check-run = %v", checkRun)
	}
	if status["id"] != float64(41) || status["state"] != "success" || status["context"] != "build/status" || status["sha"] != acceptanceHeadSHA {
		t.Errorf("recovered commit status = %v", status)
	}
	if review["id"] != float64(51) || review["state"] != "APPROVED" || review["reviewer"].(map[string]any)["login"] != "reviewer" || review["commitSha"] != acceptanceHeadSHA {
		t.Errorf("recovered review = %v", review)
	}
	if lifecycle["state"] != "MERGED" || lifecycle["action"] != "closed" || lifecycle["headSha"] != acceptanceHeadSHA {
		t.Errorf("recovered lifecycle = %v", lifecycle)
	}

	// The four persisted facts arrive through the worker's normal notice and History receipt path.
	eventually(t, "a notice submitted to Dispatcher", func() bool { return len(dispatcher.commands()) > 0 })
	submitted := dispatcher.commands()
	for _, again := range submitted[1:] {
		if again != submitted[0] {
			t.Fatalf("notice retry %+v differs from initial submission %+v", again, submitted[0])
		}
	}
	if got := hint(t, submitted[0].text); got.InboxID != inboxID || got.ThroughAtPreparation != 4 {
		t.Fatalf("notice hint = %+v, want inbox %s through cursor 4", got, inboxID)
	}
	history.add(
		runnerEntry(1, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{
			Command: &commandpb.Command{CommandId: submitted[0].id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: submitted[0].text}}},
		}}}),
		runnerEntry(2, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
			OriginCommandIds: []string{submitted[0].id},
		}}}),
	)
	entriesPath := "/v1/inboxes/" + inboxID + "/entries?limit=128&after_cursor=0"
	var confirmed map[string]any
	eventually(t, "History confirmation of the notice", func() bool {
		code, page := agentCall(t, service, http.MethodGet, entriesPath, sandboxToken, nil)
		if code != http.StatusOK {
			t.Fatalf("read confirmed inbox = %d %v", code, page)
		}
		confirmed = page
		return at(t, page, "notice", "confirmed") == true
	})
	if at(t, confirmed, "notice", "admitted") != true || at(t, confirmed, "notice", "commandId") != submitted[0].id {
		t.Errorf("notice state = %v, want the submitted command admitted and confirmed", at(t, confirmed, "notice"))
	}
	if got := inboxEntries(t, service, inboxID); !reflect.DeepEqual(got, recovered) {
		t.Fatalf("History reconciliation changed recovered entries: %v", got)
	}
	lastCursor := at(t, recovered, 3, "cursor")
	code, acknowledged := agentCall(t, service, http.MethodPut, "/v1/inboxes/"+inboxID+"/acknowledgement", sandboxToken,
		map[string]any{"throughCursor": lastCursor})
	if code != http.StatusOK || at(t, acknowledged, "inbox", "acknowledged") != lastCursor {
		t.Fatalf("acknowledge through %v = %d %v", lastCursor, code, acknowledged)
	}

	// Late matching native webhooks traverse signed HTTP ingress after polling recovered the same facts.
	webhookBase := map[string]any{
		"installation": map[string]any{"id": acceptanceInstallation},
		"repository":   map[string]any{"id": acceptanceRepositoryID, "full_name": "owner/repo"},
	}
	checkWebhook := map[string]any{"action": "completed", "check_run": map[string]any{
		"id": 31, "name": "build", "head_sha": acceptanceHeadSHA, "status": "completed", "conclusion": "success",
		"details_url": "https://github.example.test/checks/31", "started_at": "2026-10-27T00:00:00Z", "completed_at": "2026-10-27T00:01:00Z",
	}}
	statusWebhook := map[string]any{
		"sha": acceptanceHeadSHA, "id": 41, "state": "success", "context": "build/status",
		"target_url": "https://github.example.test/status", "updated_at": "2026-10-27T00:01:00Z",
	}
	reviewWebhook := map[string]any{"action": "submitted", "pull_request": map[string]any{
		"number": acceptancePullRequest, "head": map[string]any{"sha": acceptanceHeadSHA},
	}, "review": map[string]any{
		"id": 51, "user": map[string]any{"id": 101, "login": "reviewer"}, "state": "approved",
		"commit_id": acceptanceHeadSHA, "submitted_at": "2026-10-27T00:02:00Z", "body": "Looks good",
		"html_url": "https://github.example.test/reviews/51",
	}}
	lifecycleWebhook := map[string]any{"action": "closed", "pull_request": map[string]any{
		"number": acceptancePullRequest, "state": "closed", "merged": true,
		"head": map[string]any{"sha": acceptanceHeadSHA}, "base": map[string]any{"ref": "main"},
	}}
	for event, details := range map[string]map[string]any{
		"check_run": checkWebhook, "status": statusWebhook, "pull_request_review": reviewWebhook, "pull_request": lifecycleWebhook,
	} {
		payload := make(map[string]any, len(webhookBase)+len(details))
		for key, value := range webhookBase {
			payload[key] = value
		}
		for key, value := range details {
			payload[key] = value
		}
		postSignedGitHubDelivery(t, service, event, payload)
	}
	if got := inboxEntries(t, service, inboxID); !reflect.DeepEqual(got, recovered) {
		t.Fatalf("late webhooks changed the recovered identities, cursors or payloads: %v", got)
	}
	var deliveries int
	if err := service.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_delivery WHERE app_id = 42`).Scan(&deliveries); err != nil {
		t.Fatal(err)
	}
	if deliveries != 4 {
		t.Errorf("accepted late webhook receipts = %d, want four", deliveries)
	}

	// Expire the same persisted repair deadlines once more while stopped. Startup polls unchanged current state;
	// the recovered identities, cursors and payloads must remain stable.
	before := githubSubscription(t, service, subscriptionID)["lastSuccessAt"]
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Fatalf("the post-webhook service stopped with %v, want cancellation", err)
	}
	counts := map[string]int{}
	for _, path := range []string{acceptancePRPath, acceptanceChecksPath, acceptanceStatusesPath, acceptanceReviewsPath} {
		counts[path] = api.Count(path)
	}
	advanceGitHubAcceptanceDeadlines(t, service, subscriptionID, inboxID)
	service = restart(t, tune)
	eventually(t, "unchanged scheduled GitHub polls after restart", func() bool {
		for path, previous := range counts {
			if api.Count(path) <= previous {
				return false
			}
		}
		return githubSubscription(t, service, subscriptionID)["lastSuccessAt"] != before
	})
	if got := inboxEntries(t, service, inboxID); !reflect.DeepEqual(got, recovered) {
		t.Fatalf("unchanged polls changed recovered identities, cursors or payloads: %v", got)
	}
}
