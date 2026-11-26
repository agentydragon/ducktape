package facts

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestProjectPreservesMeaningfulFieldsWithoutCopyingRawBody(t *testing.T) {
	body := []byte(`{"action":"created","state":"unrelated root field","installation":{"id":11,"secret":"omit"},"repository":{"id":100,"full_name":"owner/repo","private":true},"issue":{"number":7,"title":"Title","body":"issue text","pull_request":{"url":"https://api.github.test/pr/7","large":"omit"},"unknown":"omit"},"comment":{"id":9,"body":"original comment text","html_url":"https://github.test/comment/9","user":{"id":3,"login":"alice","email":"omit"},"unknown":"omit"},"sender":{"id":4,"login":"bob","email":"omit"},"unknown":"complete raw body"}`)
	got, err := Project("issue_comment", body)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err := json.Unmarshal(got, &value); err != nil {
		t.Fatal(err)
	}
	if value["action"] != "created" {
		t.Fatalf("action = %v, want created", value["action"])
	}
	if value["state"] != nil {
		t.Fatalf("unrelated root state leaked into issue-comment projection: %v", value["state"])
	}
	comment := value["comment"].(map[string]any)
	if comment["body"] != "original comment text" {
		t.Fatalf("comment body = %v", comment["body"])
	}
	issue := value["issue"].(map[string]any)
	if issue["number"] != float64(7) || issue["body"] != "issue text" {
		t.Fatalf("issue projection = %#v", issue)
	}
	if strings.Contains(string(got), "complete raw body") || strings.Contains(string(got), "secret") ||
		strings.Contains(string(got), "email") || strings.Contains(string(got), "unknown") {
		t.Fatalf("projection copied unrelated raw fields: %s", got)
	}
}

func TestProjectPreservesStatusAndPushRootFacts(t *testing.T) {
	status, err := Project("status", []byte(`{"sha":"`+strings.Repeat("a", 40)+`","context":"ci/test","state":"failure","description":"tests failed","target_url":"https://github.test/run/1","unknown":"omit"}`))
	if err != nil {
		t.Fatal(err)
	}
	var statusValue map[string]any
	if err := json.Unmarshal(status, &statusValue); err != nil {
		t.Fatal(err)
	}
	if statusValue["sha"] != strings.Repeat("a", 40) || statusValue["context"] != "ci/test" || statusValue["state"] != "failure" ||
		statusValue["description"] != "tests failed" || statusValue["target_url"] != "https://github.test/run/1" || statusValue["unknown"] != nil {
		t.Fatalf("status projection = %#v", statusValue)
	}

	push, err := Project("push", []byte(`{"ref":"refs/heads/main","before":"`+strings.Repeat("b", 40)+`","after":"`+strings.Repeat("c", 40)+`","created":false,"deleted":false,"head_commit":{"id":"`+strings.Repeat("c", 40)+`","message":"update","author":{"name":"Alice","email":"omit","username":"alice"}},"state":"unrelated root field"}`))
	if err != nil {
		t.Fatal(err)
	}
	var pushValue map[string]any
	if err := json.Unmarshal(push, &pushValue); err != nil {
		t.Fatal(err)
	}
	if pushValue["before"] != strings.Repeat("b", 40) || pushValue["after"] != strings.Repeat("c", 40) || pushValue["ref"] != "refs/heads/main" || pushValue["state"] != nil {
		t.Fatalf("push projection = %#v", pushValue)
	}
	commit := pushValue["head_commit"].(map[string]any)
	author := commit["author"].(map[string]any)
	if author["name"] != "Alice" || author["username"] != "alice" || author["email"] != nil {
		t.Fatalf("push author projection = %#v", author)
	}
}

func TestProjectPreservesExactCommitSHAAndWorkflowJobSteps(t *testing.T) {
	sha := strings.Repeat("a", 40)
	body := []byte(`{"action":"completed","repository":{"id":100,"full_name":"owner/repo"},"workflow_job":{"id":21,"run_id":22,"head_sha":"` + sha + `","status":"completed","conclusion":"failure","steps":[{"number":1,"name":"test","status":"completed","conclusion":"failure","internal":"omit"}],"unknown":"omit"}}`)
	got, err := Project("workflow_job", body)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err := json.Unmarshal(got, &value); err != nil {
		t.Fatal(err)
	}
	job := value["workflow_job"].(map[string]any)
	if job["head_sha"] != sha || job["id"] != float64(21) || job["run_id"] != float64(22) {
		t.Fatalf("job projection = %#v", job)
	}
	step := job["steps"].([]any)[0].(map[string]any)
	if step["name"] != "test" || step["conclusion"] != "failure" || step["internal"] != nil {
		t.Fatalf("step projection = %#v", step)
	}
}

func TestProjectFiltersNestedRepositoriesAndPullRequestRefs(t *testing.T) {
	body := []byte(`{"action":"synchronize","repository":{"id":100,"full_name":"owner/repo"},"pull_request":{"id":2,"number":7,"title":"Update","body":"details","head":{"sha":"` + strings.Repeat("b", 40) + `","ref":"feature","repo":{"id":200,"full_name":"fork/repo","private":true,"secret":"omit"}},"base":{"sha":"` + strings.Repeat("c", 40) + `","ref":"main","repo":{"id":100,"full_name":"owner/repo","private":true}},"unknown":"omit"}}`)
	got, err := Project("pull_request", body)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err := json.Unmarshal(got, &value); err != nil {
		t.Fatal(err)
	}
	pr := value["pull_request"].(map[string]any)
	head := pr["head"].(map[string]any)
	if head["sha"] != strings.Repeat("b", 40) || head["ref"] != "feature" {
		t.Fatalf("head = %#v", head)
	}
	repo := head["repo"].(map[string]any)
	if repo["id"] != float64(200) || repo["full_name"] != "fork/repo" || repo["secret"] != nil {
		t.Fatalf("head repository = %#v", repo)
	}
	if strings.Contains(string(got), "unknown") || strings.Contains(string(got), "private") {
		t.Fatalf("projection copied unrelated nested fields: %s", got)
	}
}

func TestProjectRejectsUnknownEventAndNonObject(t *testing.T) {
	for _, tc := range []struct {
		event string
		body  []byte
	}{{"not_supported", []byte(`{}`)}, {"issue_comment", []byte(`[]`)}} {
		if _, err := Project(tc.event, tc.body); err == nil {
			t.Errorf("Project(%q, %s) succeeded", tc.event, tc.body)
		}
	}
}
