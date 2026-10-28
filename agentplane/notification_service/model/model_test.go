package model

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestIsCommitSHA(t *testing.T) {
	for _, test := range []struct {
		sha  string
		want bool
	}{
		{sha: strings.Repeat("a", 40), want: true},
		{sha: strings.ToUpper(strings.Repeat("a", 40))},
		{sha: strings.Repeat("g", 40)},
		{sha: strings.Repeat("a", 39)},
	} {
		if got := IsCommitSHA(test.sha); got != test.want {
			t.Errorf("IsCommitSHA(%q) = %v, want %v", test.sha, got, test.want)
		}
	}
}

func TestMergeabilityChangedIsAPullRequestOnlyActionlessSelector(t *testing.T) {
	pr := GitHubSource{Repository: "test-owner/test-repo", Subject: PullRequestSubject{Number: 7}}
	for _, subject := range []GitHubSubject{PullRequestSubject{Number: 7}} {
		source := GitHubSource{Repository: pr.Repository, Subject: subject, Events: []EventFilter{{Event: EventMergeabilityChanged}}}
		if err := source.validate(); err != nil {
			t.Fatalf("pull request selector: %v", err)
		}
	}
	for _, actions := range [][]string{{"opened"}, {}} {
		source := GitHubSource{Repository: pr.Repository, Subject: pr.Subject, Events: []EventFilter{{Event: EventMergeabilityChanged, Actions: actions}}}
		if err := source.validate(); err == nil {
			t.Errorf("selector with actions %v was accepted", actions)
		}
	}
	for _, subject := range []GitHubSubject{
		IssueSubject{Number: 7}, BranchSubject{Name: "test-branch"}, CommitSubject{SHA: strings.Repeat("a", 40)},
	} {
		source := GitHubSource{Repository: pr.Repository, Subject: subject, Events: []EventFilter{{Event: EventMergeabilityChanged}}}
		if err := source.validate(); err == nil {
			t.Errorf("selector was accepted for %T", subject)
		}
	}
	found := false
	for _, filter := range pr.Filters() {
		found = found || filter.Event == EventMergeabilityChanged
	}
	if !found {
		t.Fatal("default pull-request events omit mergeability_changed")
	}
}

func TestGitHubCommitObservationsUseNativeCamelCasePayloadFields(t *testing.T) {
	conclusion := "success"
	startedAt := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
	checkRun := GitHubCheckRun{
		RepositoryID: 22, HeadSHA: strings.Repeat("a", 40), AppID: 1001, ID: 42, Name: "build",
		Status: CheckRunCompleted, Conclusion: &conclusion,
		DetailsURL: "https://example.test/checks/42", StartedAt: &startedAt,
	}
	encoded, err := json.Marshal(checkRun)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"repositoryId":22,"headSha":"` + strings.Repeat("a", 40) + `","id":42,"appId":1001,"name":"build","status":"completed","conclusion":"success","detailsUrl":"https://example.test/checks/42","startedAt":"2026-10-27T10:00:00Z"}`
	requireEqual(t, string(encoded), want)

	commitStatus := GitHubCommitStatus{
		RepositoryID: 22, SHA: strings.Repeat("b", 40), ID: 52, Context: "ci/unit",
		State: CommitStatusSuccess, TargetURL: "https://example.test/status/52",
	}
	encoded, err = json.Marshal(commitStatus)
	if err != nil {
		t.Fatal(err)
	}
	want = `{"repositoryId":22,"sha":"` + strings.Repeat("b", 40) + `","id":52,"context":"ci/unit","state":"success","targetUrl":"https://example.test/status/52"}`
	requireEqual(t, string(encoded), want)
}

func TestGitHubCommitObservationSelectionIsPROnlyAndRespectsCheckRunPolicy(t *testing.T) {
	for name, source := range map[string]GitHubSource{
		"branch default": {Repository: "owner/repo", Subject: BranchSubject{Name: "main"}},
		"commit with explicit selectors": {
			Repository: "owner/repo", Subject: CommitSubject{SHA: strings.Repeat("a", 40)},
			Events: []EventFilter{{Event: EventCheckRun}, {Event: EventStatus}},
		},
	} {
		if source.CheckRunObservationsSelected() || source.CommitStatusObservationsSelected() {
			t.Errorf("%s source selected generated PR commit facts", name)
		}
	}
	defaultPR := GitHubSource{Repository: "owner/repo", Subject: PullRequestSubject{Number: 1}}
	if !defaultPR.CheckRunObservationsSelected() || !defaultPR.CommitObservationSelection().CompletedCheckRunsOnly ||
		!defaultPR.CommitStatusObservationsSelected() {
		t.Error("default PR source should select completed checks and commit statuses")
	}
	unfiltered := GitHubSource{Repository: "owner/repo", Subject: PullRequestSubject{Number: 1}, Events: []EventFilter{{Event: EventCheckRun}}}
	if !unfiltered.CheckRunObservationsSelected() || unfiltered.CommitObservationSelection().CompletedCheckRunsOnly {
		t.Error("unfiltered check-run PR source should select all native check-run states")
	}
	actionOnly := GitHubSource{Repository: "owner/repo", Subject: PullRequestSubject{Number: 1}, Events: []EventFilter{{Event: EventCheckRun, Actions: []string{"created"}}}}
	if actionOnly.CheckRunObservationsSelected() {
		t.Error("action-filtered check-run PR source should select raw actions only")
	}
}

func requireEqual[T comparable](t *testing.T, got, want T) {
	t.Helper()
	if got != want {
		t.Errorf("got %#v, want %#v", got, want)
	}
}
