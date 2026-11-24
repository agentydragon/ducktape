package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"regexp"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
)

const (
	ducktapeRepository  = "agentydragon/ducktape"
	trustedForkWorkflow = "trusted-fork-pr-ci.yml"
	liveTestContext     = "BuildBuddy / Bazel ordinary tests (live)"
)

var (
	// The namespace and identity format are shared with ci/invocation_ids.py.
	bazelCINamespace = uuid.MustParse("6f9619ff-8b86-d011-b42d-00c04fc964ff")
	gitCommitPattern = regexp.MustCompile("^[a-f0-9]{40}$")
)

func prStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "pr-status <pr-number>",
		Short: "Show the latest trusted Ducktape fork PR's live Bazel test status",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			number, err := strconv.Atoi(args[0])
			if err != nil || number < 1 {
				return fmt.Errorf("expected a positive Ducktape pull request number, got %q", args[0])
			}
			ctx, cancel := context.WithTimeout(cmd.Context(), requestTimeout)
			defer cancel()
			client, err := newClient()
			if err != nil {
				return err
			}
			defer client.close()
			get := func(path string, out any) error { return readGitHub(ctx, path, out) }
			findSource := func(id string) (string, error) {
				resp, err := getInvocationStatus(client, id)
				if err != nil {
					return "", err
				}
				for _, invocation := range resp.GetInvocation() {
					if invocation.GetId().GetInvocationId() == id && invocation.GetCommand() == "test" {
						return invocation.GetCommitSha(), nil
					}
				}
				return "", nil
			}
			report, err := inspectForkPR(get, findSource, number)
			if err != nil {
				return err
			}
			out := cmd.OutOrStdout()
			if jsonOutput {
				encoder := json.NewEncoder(out)
				encoder.SetIndent("", "  ")
				return encoder.Encode(report)
			}
			report.print(out)
			return nil
		},
	}
}

func readGitHub(ctx context.Context, path string, out any) error {
	raw, err := exec.CommandContext(ctx, "gh", "api", "--hostname", "github.com", "repos/"+ducktapeRepository+"/"+path).Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return fmt.Errorf("gh api %s: %w: %s", path, err, exit.Stderr)
		}
		return fmt.Errorf("gh api %s: %w", path, err)
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("decode GitHub %s: %w", path, err)
	}
	return nil
}

type forkPull struct {
	State     string
	Merge     string `json:"merge_commit_sha"`
	Mergeable *bool
	Head      struct {
		SHA  string
		Repo struct {
			FullName string `json:"full_name"`
		}
	}
	Base struct{ Ref string }
}

type forkRun struct {
	ID         int64     `json:"id"`
	Attempt    int       `json:"run_attempt"`
	Head       string    `json:"head_sha"`
	Title      string    `json:"display_title"`
	Created    time.Time `json:"created_at"`
	Status     string    `json:"status"`
	Conclusion string    `json:"conclusion"`
}

func (r forkRun) url() string {
	return fmt.Sprintf("https://github.com/%s/actions/runs/%d/attempts/%d", ducktapeRepository, r.ID, r.Attempt)
}

func (r forkRun) invocationID() string {
	return uuid.NewSHA1(bazelCINamespace, []byte(fmt.Sprintf("ducktape/bazel-ci/%d/%d/test", r.ID, r.Attempt))).String()
}

type forkPRStatus struct {
	Head               string     `json:"head"`
	Workflow           string     `json:"workflow_url,omitempty"`
	RunID              int64      `json:"run_id,omitempty"`
	Attempt            int        `json:"attempt,omitempty"`
	WorkflowState      string     `json:"workflow_state,omitempty"`
	WorkflowConclusion string     `json:"workflow_conclusion,omitempty"`
	CurrentMerge       string     `json:"current_merge,omitempty"`
	TestedMerge        string     `json:"tested_merge,omitempty"`
	CurrentBase        string     `json:"current_base,omitempty"`
	TestedBase         string     `json:"tested_base,omitempty"`
	State              string     `json:"state"`
	Description        string     `json:"description"`
	StatusURL          string     `json:"status_url,omitempty"`
	PublishedAt        *time.Time `json:"published_at,omitempty"`
}

func (s forkPRStatus) print(out io.Writer) {
	fmt.Fprintf(out, "PR head: %s\n", s.Head)
	if s.Workflow != "" {
		fmt.Fprintf(out, "Trusted fork workflow: %s %s (%s)\n", s.WorkflowState, s.WorkflowConclusion, s.Workflow)
	}
	if s.TestedMerge != "" {
		fmt.Fprintf(out, "Tested merge: %s (base %s)\n", s.TestedMerge, s.TestedBase)
	}
	if s.TestedMerge != "" && s.TestedMerge != s.CurrentMerge {
		if s.CurrentMerge == "" {
			fmt.Fprintln(out, "GitHub has not resolved a current PR merge.")
		} else {
			fmt.Fprintf(out, "GitHub's current PR merge is %s\n", s.CurrentMerge)
		}
	}
	if s.TestedBase != "" && s.CurrentBase != "" && s.TestedBase != s.CurrentBase {
		fmt.Fprintf(out, "The tested merge uses a different base from devel tip %s\n", s.CurrentBase)
	}
	fmt.Fprintf(out, "Bazel attempt: %s. %s\n", s.State, s.Description)
	if s.PublishedAt != nil {
		fmt.Fprintf(out, "%s (%s)\n", s.StatusURL, s.PublishedAt.Format(time.RFC3339))
	}
}

type githubReader func(path string, out any) error

func readForkRun(get githubReader, number int, head string) (forkRun, error) {
	title := fmt.Sprintf("Trusted PR #%d @ %s", number, head)
	var latest forkRun
	for page := 1; ; page++ {
		var runs struct {
			Total int       `json:"total_count"`
			Runs  []forkRun `json:"workflow_runs"`
		}
		path := fmt.Sprintf("actions/workflows/%s/runs?event=pull_request_target&head_sha=%s&per_page=100&page=%d", trustedForkWorkflow, head, page)
		if err := get(path, &runs); err != nil {
			return forkRun{}, err
		}
		for _, run := range runs.Runs {
			if run.Head == head && run.Title == title && run.Attempt > 0 &&
				(run.Created.After(latest.Created) || run.Created.Equal(latest.Created) && run.ID > latest.ID) {
				latest = run
			}
		}
		// GitHub orders workflow runs newest first. Follow pages if unrelated runs
		// for the same commit filled the page without matching this PR.
		if latest.ID != 0 || len(runs.Runs) == 0 || page*100 >= runs.Total {
			return latest, nil
		}
	}
}

func inspectForkPR(get githubReader, findSource func(string) (string, error), number int) (forkPRStatus, error) {
	path := fmt.Sprintf("pulls/%d", number)
	var pull forkPull
	if err := get(path, &pull); err != nil {
		return forkPRStatus{}, err
	}
	report := forkPRStatus{Head: pull.Head.SHA, CurrentMerge: pull.Merge, State: "pending"}
	if pull.State != "open" || pull.Base.Ref != "devel" || pull.Head.Repo.FullName == "" || pull.Head.Repo.FullName == ducktapeRepository {
		return report, fmt.Errorf("PR #%d must be an open Ducktape fork PR targeting devel", number)
	}
	if !gitCommitPattern.MatchString(pull.Head.SHA) {
		return report, fmt.Errorf("GitHub returned an invalid head for PR #%d", number)
	}
	if pull.Mergeable != nil && !*pull.Mergeable {
		report.State, report.Description = "conflict", "GitHub reports a merge conflict."
		return report, nil
	}
	run, err := readForkRun(get, number, report.Head)
	if err != nil {
		return report, err
	}
	if run.ID == 0 {
		report.Description = "Waiting for a trusted workflow for this head."
		return report, nil
	}
	report.Workflow, report.RunID, report.Attempt = run.url(), run.ID, run.Attempt
	report.WorkflowState, report.WorkflowConclusion = run.Status, run.Conclusion
	id := run.invocationID()
	source, err := findSource(id)
	if err != nil {
		return report, err
	}
	if source == "" {
		// The pending status is published before StartInvocation. It can be found
		// on GitHub's current merge and must still link to this exact attempt.
		source = pull.Merge
	}
	report.Description = "Waiting for a live status for this workflow attempt."
	if source == "" {
		return report, nil
	}
	if !gitCommitPattern.MatchString(source) {
		return report, fmt.Errorf("invalid tested merge SHA %q", source)
	}
	var commit struct{ Parents []struct{ SHA string } }
	if err := get("git/commits/"+source, &commit); err != nil {
		return report, err
	}
	if len(commit.Parents) != 2 || commit.Parents[1].SHA != report.Head {
		report.State, report.Description = "stale", "The merge commit cannot be matched to this PR head."
		return report, nil
	}
	var base struct{ Object struct{ SHA string } }
	if err := get("git/ref/heads/devel", &base); err != nil {
		return report, err
	}
	report.CurrentBase = base.Object.SHA
	var combined struct {
		Statuses []struct {
			Context     string
			State       string
			Description string
			Target      string    `json:"target_url"`
			Published   time.Time `json:"created_at"`
			Creator     struct{ Login string }
		}
	}
	if err := get("commits/"+source+"/status", &combined); err != nil {
		return report, err
	}
	for _, status := range combined.Statuses {
		if status.Context != liveTestContext || status.Creator.Login != "github-actions[bot]" {
			continue
		}
		if status.Target != defaultBaseURL+"/invocation/"+id && status.Target != run.url() {
			report.State, report.Description = "stale", "The merge's live status belongs to a different workflow attempt."
			break
		}
		report.TestedMerge, report.TestedBase = source, commit.Parents[0].SHA
		report.State, report.Description = status.State, status.Description
		report.StatusURL, report.PublishedAt = status.Target, &status.Published
		break
	}

	// A fetch may race a synchronize event or a rerun. Recheck both current
	// identities before attributing what we found to the PR.
	var current forkPull
	if err := get(path, &current); err != nil {
		return report, err
	}
	if current.State != "open" || current.Head.SHA != pull.Head.SHA || current.Base.Ref != pull.Base.Ref {
		return forkPRStatus{Head: current.Head.SHA, State: "stale", Description: "The PR changed while reading. Run again."}, nil
	}
	latest, err := readForkRun(get, number, current.Head.SHA)
	if err != nil {
		return report, err
	}
	if latest.ID != run.ID || latest.Attempt != run.Attempt {
		return forkPRStatus{Head: current.Head.SHA, State: "stale", Description: "The workflow attempt changed while reading. Run again."}, nil
	}
	report.CurrentMerge = current.Merge
	report.WorkflowState, report.WorkflowConclusion = latest.Status, latest.Conclusion
	return report, nil
}
