package client

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

const installationID = 11

func TestRequestsUseTheConfiguredEndpointAndVersion(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Prefix = "/api/v3"
	fake.APIVersion = "2026-01-01"
	client := newTestClient(t, fake, func(s *settings.Settings) { s.APIVersion = "2026-01-01" })

	// The fake fails the test unless every request, including the token request, carries the version and
	// credentials, so each kind of request has to be made.
	if _, err := client.Installation(t.Context(), "owner/repo"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Repository(t.Context(), "owner/repo", installationID); err != nil {
		t.Fatal(err)
	}
	if fake.Count(fixture.TokenPath) != 1 || fake.Count("/repos/owner/repo/installation") != 1 {
		t.Errorf("token requests %d, installation requests %d", fake.Count(fixture.TokenPath), fake.Count("/repos/owner/repo/installation"))
	}
}

func TestReadsReturnTheirResources(t *testing.T) {
	client := newTestClient(t, fixture.NewFake(t))
	ctx := t.Context()

	id, err := client.Installation(ctx, "owner/repo")
	if err != nil || id != installationID {
		t.Errorf("Installation = %d, %v", id, err)
	}
	for name, read := range map[string]func() (Repository, error){
		"by name": func() (Repository, error) { return client.Repository(ctx, "owner/repo", installationID) },
		"by ID":   func() (Repository, error) { return client.RepositoryByID(ctx, 100, installationID) },
	} {
		if repository, err := read(); err != nil || repository != (Repository{ID: 100, FullName: "owner/repo"}) {
			t.Errorf("repository %s = %+v, %v", name, repository, err)
		}
	}
	pullRequest, err := client.PullRequest(ctx, "owner/repo", 7, installationID)
	if err != nil || pullRequest.Head.SHA != fixture.SHA || pullRequest.Head.Repository == nil ||
		*pullRequest.Head.Repository != (Repository{ID: 200, FullName: "fork/repo"}) || pullRequest.BaseRef != "main" ||
		pullRequest.BaseSHA != fixture.BaseSHA || pullRequest.State != model.GitHubPullRequestOpen ||
		pullRequest.Mergeability != model.GitHubMergeable || pullRequest.MergeCommit == nil ||
		*pullRequest.MergeCommit != (model.GitHubMergeCommit{SHA: fixture.MergeCommitSHA, BaseSHA: fixture.BaseSHA, HeadSHA: fixture.SHA}) {
		t.Errorf("PullRequest = %+v, %v", pullRequest, err)
	}
	if err := client.CheckIssue(ctx, "owner/repo", 8, installationID); err != nil {
		t.Errorf("CheckIssue: %v", err)
	}
	if err := client.CheckCommit(ctx, "owner/repo", fixture.SHA, installationID); err != nil {
		t.Errorf("CheckCommit: %v", err)
	}
}

func TestPullRequestReportsLifecycleAndMergeabilityStates(t *testing.T) {
	for _, test := range []struct {
		name          string
		state         string
		merged        string
		mergeable     string
		mergeCommit   string
		wantState     model.GitHubPullRequestState
		wantMergeable model.GitHubMergeableState
		wantProof     bool
		wantRefReads  int
		wantTestReads int
	}{
		{
			name: "mergeable", state: "open", merged: "false", mergeable: "true", mergeCommit: fixture.MergeCommitSHA,
			wantState: model.GitHubPullRequestOpen, wantMergeable: model.GitHubMergeable, wantProof: true, wantRefReads: 1, wantTestReads: 1,
		},
		{
			name: "conflicting", state: "open", merged: "false", mergeable: "false",
			wantState: model.GitHubPullRequestOpen, wantMergeable: model.GitHubConflicting, wantRefReads: 1,
		},
		{
			name: "mergeability is being computed", state: "open", merged: "false", mergeable: "null",
			wantState: model.GitHubPullRequestOpen, wantMergeable: model.GitHubMergeabilityUnknown, wantRefReads: 1,
		},
		{
			name: "closed unmerged", state: "closed", merged: "false", mergeable: "null",
			wantState: model.GitHubPullRequestClosed, wantMergeable: model.GitHubMergeabilityUnknown,
		},
		{
			name: "merged", state: "closed", merged: "true", mergeable: "true", mergeCommit: fixture.MergeCommitSHA,
			wantState: model.GitHubPullRequestMerged, wantMergeable: model.GitHubMergeable,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			mergeCommitField := ""
			if test.mergeCommit != "" {
				mergeCommitField = `,"merge_commit_sha":"` + test.mergeCommit + `"`
			}
			fake.Respond("/repos/owner/repo/pulls/7", 200, nil,
				`{"number":7,"state":"`+test.state+`","merged":`+test.merged+`,"base":{"ref":"main","sha":"`+fixture.BaseSHA+`"},"mergeable":`+test.mergeable+mergeCommitField+`,"head":{"sha":"`+fixture.SHA+`","repo":null}}`)
			observation, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
			if err != nil {
				t.Fatal(err)
			}
			if observation.Head != (Revision{SHA: fixture.SHA}) || observation.BaseRef != "main" ||
				observation.State != test.wantState || observation.Mergeability != test.wantMergeable {
				t.Errorf("PullRequest = %+v", observation)
			}
			if observation.BaseSHA != fixture.BaseSHA {
				t.Errorf("pull request base SHA = %q, want %q", observation.BaseSHA, fixture.BaseSHA)
			}
			if (observation.MergeCommit != nil) != test.wantProof {
				t.Errorf("merge proof = %+v, want present %v", observation.MergeCommit, test.wantProof)
			}
			if got := fake.Count("/repos/owner/repo/pulls/7"); got != 1 {
				t.Errorf("%d pull request reads, want one", got)
			}
			if got := fake.Count("/repos/owner/repo/git/ref/heads/main"); got != test.wantRefReads {
				t.Errorf("%d base ref reads, want %d", got, test.wantRefReads)
			}
			if got := fake.Count("/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA); got != test.wantTestReads {
				t.Errorf("%d test-merge reads, want %d", got, test.wantTestReads)
			}
		})
	}
}

func TestPullRequestRequiresItsStateAndBaseRevision(t *testing.T) {
	for name, body := range map[string]string{
		"missing number":            `{"state":"open","merged":false,"base":{"ref":"main"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"different number":          `{"number":8,"state":"open","merged":false,"base":{"ref":"main"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"missing state":             `{"number":7,"merged":false,"base":{"ref":"main","sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"missing merged state":      `{"number":7,"state":"closed","base":{"ref":"main","sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"null merged state":         `{"number":7,"state":"closed","merged":null,"base":{"ref":"main","sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"open but marked merged":    `{"number":7,"state":"open","merged":true,"base":{"ref":"main","sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"terminal missing base":     `{"number":7,"state":"closed","merged":false,"head":{"sha":"` + fixture.SHA + `"}}`,
		"terminal missing ref":      `{"number":7,"state":"closed","merged":false,"base":{"sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"terminal missing base sha": `{"number":7,"state":"closed","merged":false,"base":{"ref":"main"},"head":{"sha":"` + fixture.SHA + `"}}`,
		"unrecognized state":        `{"number":7,"state":"merged","merged":false,"base":{"ref":"main","sha":"` + fixture.BaseSHA + `"},"head":{"sha":"` + fixture.SHA + `"}}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/pulls/7", 200, nil, body)
			_, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
			if name == "different number" {
				fixture.AsError[*SourceChangedError](t, err)
			} else {
				fixture.AsError[*InvalidResponseError](t, err)
			}
		})
	}
}

func TestOpenMergeablePullRequestsNeedAValidTestMergeForTheCurrentRevisions(t *testing.T) {
	for _, test := range []struct {
		name          string
		mergeSHA      string
		commitStatus  int
		commitBody    string
		wantCommitGet int
	}{
		{name: "missing merge commit SHA"},
		{name: "test merge is no longer available", mergeSHA: fixture.MergeCommitSHA, commitStatus: http.StatusNotFound, wantCommitGet: 1},
		{name: "different commit identity", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.OtherBaseSHA, fixture.BaseSHA, fixture.SHA), wantCommitGet: 1},
		{name: "one parent", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA), wantCommitGet: 1},
		{name: "three parents", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA, fixture.SHA, fixture.OtherBaseSHA), wantCommitGet: 1},
		{name: "missing parent identity", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA, ""), wantCommitGet: 1},
		{name: "null parent", mergeSHA: fixture.MergeCommitSHA, commitBody: `{"sha":"` + fixture.MergeCommitSHA + `","parents":[null,{"sha":"` + fixture.SHA + `"}]}`, wantCommitGet: 1},
		{name: "old base parent", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.OtherBaseSHA, fixture.SHA), wantCommitGet: 1},
		{name: "different head parent", mergeSHA: fixture.MergeCommitSHA, commitBody: fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA, fixture.OtherBaseSHA), wantCommitGet: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
				fixture.MergeablePullRequestBody("main", fixture.OtherBaseSHA, fixture.SHA, "true", test.mergeSHA))
			commitPath := "/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA
			if test.commitStatus != 0 {
				fake.Respond(commitPath, test.commitStatus, nil, "")
			} else if test.commitBody != "" {
				fake.Respond(commitPath, http.StatusOK, nil, test.commitBody)
			}

			observation, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
			if err != nil {
				t.Fatal(err)
			}
			if observation.State != model.GitHubPullRequestOpen || observation.Mergeability != model.GitHubMergeabilityUnknown ||
				observation.MergeCommit != nil || observation.BaseSHA != fixture.BaseSHA {
				t.Errorf("observation = %+v, want UNKNOWN without proof at current base %s", observation, fixture.BaseSHA)
			}
			if got := fake.Count(commitPath); got != test.wantCommitGet {
				t.Errorf("test-merge reads = %d, want %d", got, test.wantCommitGet)
			}
		})
	}
}

func TestOpenPullRequestUsesActualBaseRefInsteadOfPRBaseMetadata(t *testing.T) {
	for name, test := range map[string]struct {
		mergeable string
		prBaseSHA string
	}{
		"mergeable with stale PR base":   {mergeable: "true", prBaseSHA: fixture.OtherBaseSHA},
		"conflicting with stale PR base": {mergeable: "false", prBaseSHA: fixture.OtherBaseSHA},
		"mergeable without PR base SHA":  {mergeable: "true"},
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
				fixture.MergeablePullRequestBody("main", test.prBaseSHA, fixture.SHA, test.mergeable, testMergeCommitSHAIf(test.mergeable)))
			observation, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
			if err != nil {
				t.Fatal(err)
			}
			if observation.BaseSHA != fixture.BaseSHA {
				t.Errorf("base SHA = %q, want current branch tip %s rather than PR metadata %s", observation.BaseSHA, fixture.BaseSHA, test.prBaseSHA)
			}
			want := model.GitHubConflicting
			if test.mergeable == "true" {
				want = model.GitHubMergeable
			}
			if observation.Mergeability != want {
				t.Errorf("mergeability = %s, want %s", observation.Mergeability, want)
			}
			if (observation.MergeCommit != nil) != (test.mergeable == "true") {
				t.Errorf("merge proof = %+v, want present %v", observation.MergeCommit, test.mergeable == "true")
			}
		})
	}
}

func testMergeCommitSHAIf(mergeable string) string {
	if mergeable == "true" {
		return fixture.MergeCommitSHA
	}
	return ""
}

func TestOpenPullRequestWithoutCurrentBaseRefIsUnknownWithoutInventingASHA(t *testing.T) {
	for _, test := range []struct {
		name         string
		baseRef      string
		baseStatus   int
		wantRefReads int
		wantCommit   int
	}{
		{name: "missing PR base ref"},
		{name: "base branch missing", baseRef: "main", baseStatus: http.StatusNotFound, wantRefReads: 1, wantCommit: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
				fixture.MergeablePullRequestBody(test.baseRef, fixture.OtherBaseSHA, fixture.SHA, "true", fixture.MergeCommitSHA))
			if test.baseStatus != 0 {
				fake.Respond("/repos/owner/repo/git/ref/heads/main", test.baseStatus, nil, "")
			}

			observation, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
			if err != nil {
				t.Fatal(err)
			}
			if observation.Mergeability != model.GitHubMergeabilityUnknown || observation.MergeCommit != nil || observation.BaseSHA != "" {
				t.Errorf("observation = %+v, want UNKNOWN with no base SHA or proof", observation)
			}
			if got := fake.Count("/repos/owner/repo/git/ref/heads/main"); got != test.wantRefReads {
				t.Errorf("base ref reads = %d, want %d", got, test.wantRefReads)
			}
			if got := fake.Count("/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA); got != test.wantCommit {
				t.Errorf("test-merge reads = %d, want %d", got, test.wantCommit)
			}
		})
	}
}

func TestBaseRefIsReadAfterTheTestMergeAndFencesAMovingBase(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		fixture.MergeablePullRequestBody("main", fixture.BaseSHA, fixture.SHA, "true", fixture.MergeCommitSHA))
	commitPath := "/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA
	basePath := "/repos/owner/repo/git/ref/heads/main"
	var readTestMerge bool
	fake.Handler = func(w http.ResponseWriter, _ *http.Request, path string) bool {
		switch path {
		case commitPath:
			readTestMerge = true
			fmt.Fprint(w, fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA, fixture.SHA))
			return true
		case basePath:
			if !readTestMerge {
				t.Error("base ref was read before the test merge")
			}
			fmt.Fprintf(w, `{"object":{"sha":%q}}`, fixture.OtherBaseSHA)
			return true
		default:
			return false
		}
	}

	observation, err := newTestClient(t, fake).PullRequest(t.Context(), "owner/repo", 7, installationID)
	if err != nil {
		t.Fatal(err)
	}
	if observation.BaseSHA != fixture.OtherBaseSHA || observation.Mergeability != model.GitHubMergeabilityUnknown || observation.MergeCommit != nil {
		t.Errorf("observation = %+v, want UNKNOWN after base moved to %s", observation, fixture.OtherBaseSHA)
	}
	requests := fake.RequestURIs()
	if len(requests) < 2 || !strings.HasSuffix(requests[len(requests)-2], "/commits/"+fixture.MergeCommitSHA) ||
		!strings.HasSuffix(requests[len(requests)-1], "/git/ref/heads/main") {
		t.Errorf("last requests = %v, want test merge followed by current base ref", requests)
	}
}

func TestErrorsReadingTheTestMergeStillReachTheCaller(t *testing.T) {
	for _, test := range []struct {
		name   string
		status int
		want   any
		stall  bool
	}{
		{name: "access denied", status: http.StatusForbidden, want: &AccessError{}},
		{name: "rate limited", status: http.StatusTooManyRequests, want: &RateLimitedError{}},
		{name: "transport failed", stall: true, want: &TransportError{}},
	} {
		t.Run(test.name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			commitPath := "/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA
			if test.stall {
				fake.Stall = commitPath
			} else {
				fake.Respond(commitPath, test.status, nil, "")
			}
			client := newTestClient(t, fake, func(s *settings.Settings) { s.RequestTimeoutSeconds = 0.2 })
			_, err := client.PullRequest(t.Context(), "owner/repo", 7, installationID)
			switch test.want.(type) {
			case *AccessError:
				fixture.AsError[*AccessError](t, err)
			case *RateLimitedError:
				fixture.AsError[*RateLimitedError](t, err)
			case *TransportError:
				fixture.AsError[*TransportError](t, err)
			}
		})
	}
}

func TestBranchesAreFoundByNameAndMissingOnesAreNotAnError(t *testing.T) {
	fake := fixture.NewFake(t)
	client := newTestClient(t, fake)
	for _, branch := range []string{"devel", "feature/x"} {
		if sha, found, err := client.Branch(t.Context(), "owner/repo", branch, installationID); sha != fixture.SHA || !found || err != nil {
			t.Errorf("Branch %q = %q, %v, %v", branch, sha, found, err)
		}
	}
	if sha, found, err := client.Branch(t.Context(), "owner/repo", "gone", installationID); sha != "" || found || err != nil {
		t.Errorf("missing branch = %q, %v, %v", sha, found, err)
	}
}

// go-github keeps the slashes of a branch name and escapes each segment, so `feature/x` is requested as
// `heads/feature/x`; the Python client sent `heads/feature%2Fx`. GitHub answered both forms with the same branch
// for a real branch with a slash in its name, so the client keeps go-github's form.
func TestBranchNamesAreSentWithEachSegmentEscaped(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Handler = func(w http.ResponseWriter, _ *http.Request, path string) bool {
		if !strings.Contains(path, "/git/ref/") {
			return false
		}
		w.WriteHeader(http.StatusNotFound)
		return true
	}
	client := newTestClient(t, fake)
	const prefix = "/repos/owner/repo/git/ref/heads/"
	for branch, want := range map[string]string{
		"feature/x": "feature/x",
		"a b":       "a%20b",
		"a#b?c":     "a%23b%3Fc",
		"100%":      "100%25",
		"ünï":       "%C3%BCn%C3%AF",
	} {
		before := len(fake.RequestURIs())
		if _, found, err := client.Branch(t.Context(), "owner/repo", branch, installationID); found || err != nil {
			t.Errorf("Branch %q = %v, %v", branch, found, err)
		}
		uris := fake.RequestURIs()[before:]
		if got := uris[len(uris)-1]; got != prefix+want {
			t.Errorf("Branch %q requested %q, want %q", branch, got, prefix+want)
		}
	}
}

func TestBranchNamesThatWouldResolveOntoAnotherEndpointAreMissingBranches(t *testing.T) {
	fake := fixture.NewFake(t)
	client := newTestClient(t, fake)
	for _, branch := range []string{"../../x", "a/../b", "./x", "x/.."} {
		if sha, found, err := client.Branch(t.Context(), "owner/repo", branch, installationID); sha != "" || found || err != nil {
			t.Errorf("Branch %q = %q, %v, %v", branch, sha, found, err)
		}
	}
	if n := fake.Total(); n != 0 {
		t.Errorf("%d requests were made", n)
	}
}

func TestATokenRequestAnsweringNotFoundIsLostAccessNotAMissingBranch(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Respond(fixture.TokenPath, 404, nil, "")
	_, _, err := newTestClient(t, fake).Branch(t.Context(), "owner/repo", "devel", installationID)
	if access := fixture.AsError[*AccessError](t, err); access.StatusCode != 404 {
		t.Errorf("access error %+v", access)
	}
}

func TestAnIssueMustBeThatIssueAndNotAPullRequest(t *testing.T) {
	for name, body := range map[string]string{
		"a pull request": `{"number": 8, "pull_request": {}}`,
		"another number": `{"number": 9}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/issues/8", 200, nil, body)
			err := newTestClient(t, fake).CheckIssue(t.Context(), "owner/repo", 8, installationID)
			fixture.AsError[*SourceChangedError](t, err)
		})
	}
	t.Run("a missing issue", func(t *testing.T) {
		fake := fixture.NewFake(t)
		fake.Respond("/repos/owner/repo/issues/8", 404, nil, "")
		err := newTestClient(t, fake).CheckIssue(t.Context(), "owner/repo", 8, installationID)
		if access := fixture.AsError[*AccessError](t, err); access.StatusCode != 404 {
			t.Errorf("access error %+v", access)
		}
	})
}

func TestACommitMustBeThatCommit(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Respond("/repos/owner/repo/commits/"+fixture.SHA, 200, nil, `{"sha": "`+strings.Repeat("b", 40)+`"}`)
	err := newTestClient(t, fake).CheckCommit(t.Context(), "owner/repo", fixture.SHA, installationID)
	fixture.AsError[*SourceChangedError](t, err)
}

func TestTheInstallationLookupTellsAbsenceSuspensionAndDenialApart(t *testing.T) {
	const path = "/repos/owner/repo/installation"
	fake := fixture.NewFake(t)
	client := newTestClient(t, fake)
	var absent *NotInstalledError
	var suspended *SuspendedError

	fake.Respond(path, 404, nil, "")
	_, err := client.Installation(t.Context(), "owner/repo")
	if notInstalled := fixture.AsError[*NotInstalledError](t, err); notInstalled.StatusCode != 404 {
		t.Errorf("absent installation %+v", notInstalled)
	}
	fixture.AsError[*AccessError](t, err)
	if errors.As(err, &suspended) {
		t.Error("an absent installation is not a suspended one")
	}

	fake.Respond(path, 200, nil, `{"id": 22, "suspended_at": "2026-01-02T03:04:05Z"}`)
	_, err = client.Installation(t.Context(), "owner/repo")
	fixture.AsError[*SuspendedError](t, err)
	fixture.AsError[*AccessError](t, err)
	if errors.As(err, &absent) {
		t.Error("a suspended installation is not an absent one")
	}

	for _, status := range []int{401, 403} {
		fake.Respond(path, status, nil, "")
		_, err = client.Installation(t.Context(), "owner/repo")
		if denied := fixture.AsError[*AccessError](t, err); denied.StatusCode != status {
			t.Errorf("denied installation %+v", denied)
		}
		if errors.As(err, &absent) || errors.As(err, &suspended) {
			t.Errorf("HTTP %d is neither absent nor suspended", status)
		}
	}
}

func TestAccessLostToTheInstallationOrRepositoryIsAnAccessError(t *testing.T) {
	for _, denial := range []struct {
		path   string
		status int
	}{
		{fixture.TokenPath, 401},
		{fixture.TokenPath, 403},
		{fixture.TokenPath, 404},
		{"/repos/owner/repo", 401},
		{"/repos/owner/repo", 403},
		{"/repos/owner/repo", 404},
	} {
		t.Run(denial.path+" "+strconv.Itoa(denial.status), func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond(denial.path, denial.status, nil, `{"message": "private upstream body"}`)
			_, err := newTestClient(t, fake).Repository(t.Context(), "owner/repo", installationID)
			access := fixture.AsError[*AccessError](t, err)
			if access.StatusCode != denial.status || !strings.Contains(err.Error(), "HTTP "+strconv.Itoa(denial.status)) ||
				strings.Contains(err.Error(), "private upstream body") {
				t.Errorf("error %v", err)
			}
		})
	}
}

func TestInstallationTokensAreSharedAndDroppedWhenRejected(t *testing.T) {
	fake := fixture.NewFake(t)
	client := newTestClient(t, fake)
	ctx := t.Context()

	reads := []func() error{
		func() error { _, err := client.Repository(ctx, "owner/repo", installationID); return err },
		func() error { _, err := client.PullRequest(ctx, "owner/repo", 7, installationID); return err },
		func() error { return client.CheckIssue(ctx, "owner/repo", 8, installationID) },
		func() error { _, _, err := client.Branch(ctx, "owner/repo", "devel", installationID); return err },
		func() error { return client.CheckCommit(ctx, "owner/repo", fixture.SHA, installationID) },
	}
	var wg sync.WaitGroup
	for _, read := range reads {
		wg.Go(func() {
			if err := read(); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if n := fake.Count(fixture.TokenPath); n != 1 {
		t.Errorf("concurrent reads minted %d tokens, want 1", n)
	}

	fake.Respond("/repos/owner/repo", 401, nil, "")
	_, err := client.Repository(ctx, "owner/repo", installationID)
	fixture.AsError[*AccessError](t, err)
	fake.Restore("/repos/owner/repo")
	if _, err := client.Repository(ctx, "owner/repo", installationID); err != nil {
		t.Fatal(err)
	}
	if n := fake.Count(fixture.TokenPath); n != 2 {
		t.Errorf("tokens minted after a 401 = %d, want 2", n)
	}
}

func TestRedirectsAreNeverFollowed(t *testing.T) {
	var captured atomic.Int32
	capture := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { captured.Add(1) }))
	t.Cleanup(capture.Close)
	redirect := map[string]string{"Location": capture.URL + "/capture"}

	fake := fixture.NewFake(t)
	fake.Respond("/repos/owner/repo/installation", 302, redirect, "")
	fake.Respond("/repos/owner/repo", 302, redirect, "")
	fake.Respond("/app/installations/12/access_tokens", 302, redirect, "")
	client := newTestClient(t, fake)

	_, appErr := client.Installation(t.Context(), "owner/repo")
	_, installationErr := client.Repository(t.Context(), "owner/repo", installationID)
	_, tokenErr := client.Repository(t.Context(), "owner/repo", 12)
	for name, err := range map[string]error{"App": appErr, "installation": installationErr, "token": tokenErr} {
		if status := fixture.AsError[*StatusError](t, err); status.StatusCode != 302 {
			t.Errorf("%s request: %+v", name, status)
		}
	}
	if captured.Load() != 0 {
		t.Error("a redirect was followed with the client's credentials")
	}
}

func TestRateLimitsReachTheCallerWithTheirRetryDelay(t *testing.T) {
	const body = "private upstream body"
	for name, limit := range map[string]struct {
		status      int
		header      map[string]string
		wantSeconds int
	}{
		"secondary limit with the primary exhausted": {403, map[string]string{"X-RateLimit-Remaining": "0", "Retry-After": "120"}, 120},
		"primary limit as a 403":                     {403, map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, 2600},
		"primary limit as a 429":                     {429, map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}, 2600},
		"secondary limit with only Retry-After":      {403, map[string]string{"Retry-After": "90"}, 90},
		"429 without any hint":                       {429, nil, 60},
		"Retry-After as an HTTP date":                {429, map[string]string{"Retry-After": "Thu, 01 Jan 1970 01:00:00 GMT"}, 2600},
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo/installation", limit.status, limit.header, body)
			client := newTestClient(t, fake)
			client.now = func() time.Time { return time.Unix(1000, 250_000_000) }

			_, err := client.Installation(t.Context(), "owner/repo")
			limited := fixture.AsError[*RateLimitedError](t, err)
			if limited.StatusCode != limit.status || limited.RetrySeconds != limit.wantSeconds {
				t.Errorf("%+v, want HTTP %d and %ds", limited, limit.status, limit.wantSeconds)
			}
			if strings.Contains(err.Error(), body) {
				t.Errorf("the upstream body reached the error: %v", err)
			}
		})
	}
}

func TestARateLimitedTokenRequestIsARateLimit(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Respond(fixture.TokenPath, 429, map[string]string{"Retry-After": "90"}, "")
	_, err := newTestClient(t, fake).Repository(t.Context(), "owner/repo", installationID)
	if limited := fixture.AsError[*RateLimitedError](t, err); limited.StatusCode != 429 || limited.RetrySeconds != 90 {
		t.Errorf("limited %+v", limited)
	}
}

func TestAForbiddenResponseWithoutARateLimitIsAccessLost(t *testing.T) {
	fake := fixture.NewFake(t)
	fake.Respond("/repos/owner/repo/installation", 403, map[string]string{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "3600"}, "")
	_, err := newTestClient(t, fake).Installation(t.Context(), "owner/repo")
	if access := fixture.AsError[*AccessError](t, err); access.StatusCode != 403 {
		t.Errorf("access error %+v", access)
	}
}

func TestTheClientKeepsNoRateLimitStateOfItsOwn(t *testing.T) {
	const path = "/repos/owner/repo/installation"
	fake := fixture.NewFake(t)
	reset := strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)
	fake.Respond(path, 403, map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}, "")
	client := newTestClient(t, fake)
	for range 2 {
		_, err := client.Installation(t.Context(), "owner/repo")
		fixture.AsError[*RateLimitedError](t, err)
	}
	if n := fake.Count(path); n != 2 {
		t.Errorf("GitHub was asked %d times, want 2: a limit GitHub reported must not answer for it later", n)
	}
}

func TestOtherStatusesKeepTheirCode(t *testing.T) {
	for _, status := range []int{400, 410, 422, 500, 502} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo", status, nil, "")
			_, err := newTestClient(t, fake).Repository(t.Context(), "owner/repo", installationID)
			if failure := fixture.AsError[*StatusError](t, err); failure.StatusCode != status {
				t.Errorf("failure %+v", failure)
			}
		})
	}
}

func TestResponsesTheClientCannotUseAreInvalid(t *testing.T) {
	for name, body := range map[string]string{
		"not JSON":              "<html>",
		"empty":                 "",
		"null":                  "null",
		"wrong field type":      `{"id": "100", "full_name": "owner/repo"}`,
		"no id":                 `{"full_name": "owner/repo"}`,
		"non-positive id":       `{"id": 0, "full_name": "owner/repo"}`,
		"no name":               `{"id": 100}`,
		"malformed name":        `{"id": 100, "full_name": "owner"}`,
		"name with a path":      `{"id": 100, "full_name": "owner/repo/../x"}`,
		"name that is too long": `{"id": 100, "full_name": "owner/` + strings.Repeat("r", 200) + `"}`,
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond("/repos/owner/repo", 200, nil, body)
			_, err := newTestClient(t, fake).Repository(t.Context(), "owner/repo", installationID)
			fixture.AsError[*InvalidResponseError](t, err)
		})
	}
	t.Run("installation without an id", func(t *testing.T) {
		fake := fixture.NewFake(t)
		fake.Respond("/repos/owner/repo/installation", 200, nil, `{"id": 0}`)
		_, err := newTestClient(t, fake).Installation(t.Context(), "owner/repo")
		fixture.AsError[*InvalidResponseError](t, err)
	})
	t.Run("issue without a number", func(t *testing.T) {
		fake := fixture.NewFake(t)
		fake.Respond("/repos/owner/repo/issues/8", 200, nil, `{}`)
		err := newTestClient(t, fake).CheckIssue(t.Context(), "owner/repo", 8, installationID)
		fixture.AsError[*InvalidResponseError](t, err)
	})
}

func TestATokenResponseTheClientCannotUseIsInvalid(t *testing.T) {
	for name, body := range map[string]string{"not JSON": "<html>", "empty": "", "without a token": `{"expires_at": "2099-01-01T00:00:00Z"}`} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Respond(fixture.TokenPath, 201, nil, body)
			_, err := newTestClient(t, fake).Repository(t.Context(), "owner/repo", installationID)
			fixture.AsError[*InvalidResponseError](t, err)
		})
	}
}

func TestAnUnreachableGitHubIsATransportError(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	server.Close()
	client, err := New(fixture.Settings(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Installation(t.Context(), "owner/repo")
	fixture.AsError[*TransportError](t, err)
}

func TestAStalledRequestIsATransportErrorAtTheRequestTimeout(t *testing.T) {
	for name, stalled := range map[string]string{
		"App request":   "/repos/owner/repo/installation",
		"token request": fixture.TokenPath,
		"API request":   "/repos/owner/repo",
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Stall = stalled
			client := newTestClient(t, fake, func(s *settings.Settings) { s.RequestTimeoutSeconds = 0.2 })
			var err error
			if stalled == "/repos/owner/repo/installation" {
				_, err = client.Installation(t.Context(), "owner/repo")
			} else {
				_, err = client.Repository(t.Context(), "owner/repo", installationID)
			}
			if transport := fixture.AsError[*TransportError](t, err); !errors.Is(transport, context.DeadlineExceeded) {
				t.Errorf("error %v", err)
			}
		})
	}
}

func TestACallerThatGivesUpGetsItsOwnError(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	_, err := newTestClient(t, fixture.NewFake(t)).Repository(ctx, "owner/repo", installationID)
	if !errors.Is(err, context.Canceled) {
		t.Errorf("error = %v", err)
	}
}

func TestMalformedCallsNeverReachGitHub(t *testing.T) {
	fake := fixture.NewFake(t)
	client := newTestClient(t, fake)
	for _, repository := range []string{"", "owner", "owner/repo/extra", "owner/../repo", "own er/repo", "owner/repo?x=1", "/repo", "owner/"} {
		if _, err := client.Installation(t.Context(), repository); err == nil {
			t.Errorf("repository %q was accepted", repository)
		}
	}
	for _, id := range []int64{0, -1} {
		if _, err := client.Repository(t.Context(), "owner/repo", id); err == nil {
			t.Errorf("installation %d was accepted", id)
		}
	}
	if n := fake.Total(); n != 0 {
		t.Errorf("%d requests were made", n)
	}
}

func TestNewRejectsAnUnusablePrivateKeyWithoutEchoingIt(t *testing.T) {
	ec, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(ec)
	if err != nil {
		t.Fatal(err)
	}
	for name, key := range map[string]string{
		"not PEM":    "never-print-this-key-material",
		"truncated":  fixture.KeyPEM()[:300],
		"not an RSA": string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})),
	} {
		t.Run(name, func(t *testing.T) {
			settings := fixture.Settings("https://github.test")
			settings.PrivateKey = key
			_, err := New(settings)
			// The second line of a PEM is key material; a one-line key is itself the material.
			material := strings.Split(key, "\n")[len(strings.Split(key, "\n"))/2]
			if err == nil || strings.Contains(err.Error(), material) {
				t.Errorf("error %v", err)
			}
		})
	}
}
