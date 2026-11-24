package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestForkPRStatus(t *testing.T) {
	const (
		number     = 19256
		runID      = 45099731016
		head       = "33fb799c3611e7ec13640e2f85a2dd21eba5812d"
		source     = "a475f7bd41bde8c173e2c979cf5129d9c048b861"
		base       = "ccd89a2d8ff306173b5d5cb702adab663010706e"
		invocation = "32c5739f-d582-5f87-a8be-793304e30d07"
	)
	testCases := []struct {
		name           string
		source         string
		parent         string
		statusTarget   string
		statusState    string
		statusCreator  string
		completed      bool
		currentBase    string
		advanceHead    bool
		advanceAttempt bool
		wantState      string
		wantTested     bool
		wantStaleBase  bool
	}{
		{name: "failed exact attempt", source: source, statusState: "failure", wantState: "failure", wantTested: true},
		{name: "base advanced", source: source, statusState: "failure", currentBase: strings.Repeat("e", 40), wantState: "failure", wantTested: true, wantStaleBase: true},
		{name: "finished retry", source: source, statusTarget: (forkRun{ID: runID, Attempt: 1}).url(), statusState: "success", wantState: "success", wantTested: true},
		{name: "waiting for BuildBuddy", statusState: "pending", wantState: "pending", wantTested: true},
		{name: "no status", source: source, wantState: "pending"},
		{name: "completed without status", source: source, completed: true, wantState: "unavailable"},
		{name: "different attempt", source: source, statusState: "failure", statusTarget: "https://app.buildbuddy.io/invocation/stale", wantState: "stale"},
		{name: "untrusted creator", source: source, statusState: "failure", statusCreator: "unrelated", wantState: "pending"},
		{name: "different head parent", source: source, statusState: "failure", parent: strings.Repeat("d", 40), wantState: "stale"},
		{name: "head moved while fetching", source: source, statusState: "failure", advanceHead: true, wantState: "stale"},
		{name: "reran while fetching", source: source, statusState: "failure", advanceAttempt: true, wantState: "stale"},
	}
	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			pullReads, runReads := 0, 0
			get := func(path string, out any) error {
				var body string
				switch {
				case path == "pulls/19256":
					pullReads++
					pullHead := head
					if tc.advanceHead && pullReads > 1 {
						pullHead = strings.Repeat("d", 40)
					}
					body = fmt.Sprintf(`{"state":"open","mergeable":true,"merge_commit_sha":%q,"head":{"sha":%q,"repo":{"full_name":"agentydragon-agent/ducktape"}},"base":{"ref":"devel"}}`, source, pullHead)
				case strings.HasPrefix(path, "actions/workflows/trusted-fork-pr-ci.yml/runs?"):
					runReads++
					attempt := 1
					if tc.advanceAttempt && runReads > 1 {
						attempt = 2
					}
					workflowState := "in_progress"
					if tc.completed {
						workflowState = "completed"
					}
					body = fmt.Sprintf(`{"total_count":2,"workflow_runs":[
						{"id":%d,"run_attempt":3,"head_sha":%q,"display_title":"Trusted PR #1 @ %s","created_at":"2026-11-23T02:21:26Z"},
						{"id":%d,"run_attempt":%d,"head_sha":%q,"display_title":"Trusted PR #19256 @ %s","created_at":"2026-11-23T02:21:25Z","status":%q}
					]}`, runID+1, head, head, runID, attempt, head, head, workflowState)
				case path == "git/commits/"+source:
					parent := tc.parent
					if parent == "" {
						parent = head
					}
					body = fmt.Sprintf(`{"parents":[{"sha":%q},{"sha":%q}]}`, base, parent)
				case path == "git/ref/heads/devel":
					currentBase := tc.currentBase
					if currentBase == "" {
						currentBase = base
					}
					body = fmt.Sprintf(`{"object":{"sha":%q}}`, currentBase)
				case path == "commits/"+source+"/statuses?per_page=100&page=1":
					body = `[]`
					if tc.statusState != "" {
						target := tc.statusTarget
						if target == "" {
							target = defaultBaseURL + "/invocation/" + invocation
						}
						creator := tc.statusCreator
						if creator == "" {
							creator = "github-actions[bot]"
						}
						body = fmt.Sprintf(`[{"context":%q,"state":%q,"description":"Ordinary test invocation failed","target_url":%q,"created_at":"2026-11-23T02:23:34Z","creator":{"login":%q}}]`, liveTestContext, tc.statusState, target, creator)
					}
				default:
					t.Fatalf("unexpected GitHub request: %s", path)
				}
				return json.Unmarshal([]byte(body), out)
			}
			result, err := inspectForkPR(get, func(id string) (string, error) {
				if id != invocation {
					t.Fatalf("derived invocation %q, want %q", id, invocation)
				}
				return tc.source, nil
			}, number)
			if err != nil {
				t.Fatal(err)
			}
			if result.State != tc.wantState || (result.TestedMerge == source) != tc.wantTested {
				t.Fatalf("unexpected report: %+v", result)
			}
			var printed strings.Builder
			result.print(&printed)
			if got := strings.Contains(printed.String(), "uses a different base"); got != tc.wantStaleBase {
				t.Fatalf("unexpected base reporting: %s", printed.String())
			}
		})
	}
}
