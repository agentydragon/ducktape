package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	ctxpb "github.com/buildbuddy-io/buildbuddy/proto/context"
	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"github.com/spf13/cobra"
)

func targetCmd() *cobra.Command {
	var label string
	var filter string
	cmd := &cobra.Command{
		Use:   "target <invocation-id>",
		Short: "List targets in an invocation (default), or use subcommands",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			ids, err := resolveInvocationIDs(c, args[0])
			if err != nil {
				return err
			}
			t := newTable()
			t.header("STATUS", "DUR", "RULE", "LABEL")
			for _, id := range ids {
				req := &targetpb.GetTargetRequest{
					InvocationId: id,
					TargetLabel:  label,
					Filter:       filter,
				}
				resp := &targetpb.GetTargetResponse{}
				if err := c.call("GetTarget", req, resp); err != nil {
					return err
				}
				if jsonOutput {
					return printProtoJSON(resp)
				}
				for _, g := range resp.GetTargetGroups() {
					for _, tgt := range g.GetTargets() {
						meta := tgt.GetMetadata()
						dur := fmtDurationUsec(tgt.GetTiming().GetDuration().AsDuration().Microseconds())
						lbl := meta.GetLabel()
						if tgt.GetRootCause() {
							lbl += " [ROOT CAUSE]"
						}
						t.row(tgt.GetStatus().String(), dur, meta.GetRuleType(), lbl)
					}
				}
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&label, "label", "", "Filter to specific target label")
	cmd.Flags().StringVar(&filter, "filter", "", "Substring filter on target labels")
	cmd.AddCommand(targetHistorySubCmd())
	cmd.AddCommand(targetLogSubCmd())
	cmd.AddCommand(targetStatsSubCmd())
	cmd.AddCommand(targetFlakesSubCmd())
	return cmd
}

func targetHistorySubCmd() *cobra.Command {
	var repo string
	var label string
	var failuresOnly bool
	var count int
	var since string
	cmd := &cobra.Command{
		Use:   "history",
		Short: "Show pass/fail/flake history for targets",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			if repo == "" {
				repo, err = detectRepoURL()
				if err != nil {
					return fmt.Errorf("auto-detect repo (use --repo to override): %w", err)
				}
			}
			groupID, err := c.resolveGroupID(repo)
			if err != nil {
				return err
			}
			req := &targetpb.GetTargetHistoryRequest{
				RequestContext: &ctxpb.RequestContext{
					GroupId: groupID,
				},
				Query: &targetpb.TargetQuery{
					RepoUrl: repo,
				},
				ServerSidePagination: true,
			}
			sinceTime, err := parseSince(since, time.Now())
			if err != nil {
				return fmt.Errorf("--since: %w", err)
			}

			resp, err := fetchAllTargetHistory(c, req, sinceTime)
			if err != nil {
				return err
			}
			filterTargetHistory(resp, label, failuresOnly, sinceTime, count)
			if jsonOutput {
				return printProtoJSON(resp)
			}
			for _, th := range resp.GetInvocationTargets() {
				fmt.Printf("Target: %s\n", th.GetTarget().GetLabel())
				t := newTable()
				t.header("STATUS", "DUR", "STARTED", "COMMIT", "INVOCATION")
				for _, s := range th.GetTargetStatus() {
					started := s.GetTiming().GetStartTime().AsTime().Format("2006-01-02 15:04")
					dur := fmtDurationUsec(s.GetTiming().GetDuration().AsDuration().Microseconds())
					sha := s.GetCommitSha()
					if len(sha) > 8 {
						sha = sha[:8]
					}
					t.row(s.GetStatus().String(), dur, started, sha, s.GetInvocationId())
				}
				t.flush()
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().StringVar(&label, "label", "", "Filter to specific target label")
	cmd.Flags().BoolVar(&failuresOnly, "failures-only", false, "Show only non-PASSED statuses")
	cmd.Flags().IntVar(&count, "count", 0, "Maximum number of targets to show (0 = all)")
	cmd.Flags().StringVar(&since, "since", "", "Show only entries after this time (e.g., 168h, 720h, 2026-04-01)")
	return cmd
}

func fetchAllTargetHistory(c *client, req *targetpb.GetTargetHistoryRequest, since time.Time) (*targetpb.GetTargetHistoryResponse, error) {
	result := &targetpb.GetTargetHistoryResponse{}
	indexesByLabel := make(map[string]int)
	for {
		page := &targetpb.GetTargetHistoryResponse{}
		if err := c.call("GetTargetHistory", req, page); err != nil {
			return nil, err
		}
		if !since.IsZero() && targetHistoryPageIsOlderThan(page, since) {
			break
		}
		for _, history := range page.GetInvocationTargets() {
			if !since.IsZero() {
				history.TargetStatus = filterTargetHistoryStatuses(history.GetTargetStatus(), false, since)
			}
			label := history.GetTarget().GetLabel()
			if index, ok := indexesByLabel[label]; ok {
				result.InvocationTargets[index].TargetStatus = append(
					result.InvocationTargets[index].GetTargetStatus(), history.GetTargetStatus()...,
				)
				continue
			}
			indexesByLabel[label] = len(result.InvocationTargets)
			result.InvocationTargets = append(result.InvocationTargets, history)
		}
		if page.GetNextPageToken() == "" {
			break
		}
		req.PageToken = page.GetNextPageToken()
	}
	return result, nil
}

// BuildBuddy orders commits by their maximum invocation start time, newest first, and
// puts all invocations for a page of commits together. A page is a safe cutoff only
// when every status has a valid creation time strictly before since.
func targetHistoryPageIsOlderThan(page *targetpb.GetTargetHistoryResponse, since time.Time) bool {
	statusCount := 0
	for _, history := range page.GetInvocationTargets() {
		for _, status := range history.GetTargetStatus() {
			statusCount++
			createdAtUsec := status.GetInvocationCreatedAtUsec()
			if createdAtUsec <= 0 || !time.UnixMicro(createdAtUsec).Before(since) {
				return false
			}
		}
	}
	return statusCount > 0
}

func filterTargetHistory(resp *targetpb.GetTargetHistoryResponse, label string, failuresOnly bool, since time.Time, count int) {
	filteredTargets := make([]*targetpb.TargetHistory, 0, len(resp.GetInvocationTargets()))
	for _, history := range resp.GetInvocationTargets() {
		if label != "" && history.GetTarget().GetLabel() != label {
			continue
		}
		filteredStatuses := filterTargetHistoryStatuses(history.GetTargetStatus(), failuresOnly, since)
		if len(filteredStatuses) == 0 && (failuresOnly || !since.IsZero()) {
			continue
		}
		history.TargetStatus = filteredStatuses
		filteredTargets = append(filteredTargets, history)
		if count > 0 && len(filteredTargets) >= count {
			break
		}
	}
	resp.InvocationTargets = filteredTargets
	resp.NextPageToken = ""
}

func filterTargetHistoryStatuses(statuses []*targetpb.TargetStatus, failuresOnly bool, since time.Time) []*targetpb.TargetStatus {
	filtered := make([]*targetpb.TargetStatus, 0, len(statuses))
	for _, status := range statuses {
		if failuresOnly && status.GetStatus().String() == "PASSED" {
			continue
		}
		// Use invocation creation time, not test start time (cached tests report original start).
		invocationTime := time.UnixMicro(status.GetInvocationCreatedAtUsec())
		if !since.IsZero() && invocationTime.Before(since) {
			continue
		}
		filtered = append(filtered, status)
	}
	return filtered
}

func targetStatsSubCmd() *cobra.Command {
	var repo string
	cmd := &cobra.Command{
		Use:   "stats",
		Short: "Show flake statistics for targets",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			if repo == "" {
				repo, err = detectRepoURL()
				if err != nil {
					return fmt.Errorf("auto-detect repo (use --repo to override): %w", err)
				}
			}
			groupID, err := c.resolveGroupID(repo)
			if err != nil {
				return err
			}
			req := &targetpb.GetTargetStatsRequest{
				RequestContext: &ctxpb.RequestContext{GroupId: groupID},
				Repo:           repo,
			}
			resp := &targetpb.GetTargetStatsResponse{}
			if err := c.call("GetTargetStats", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("LABEL", "TOTAL", "PASS", "FAIL", "FLAKY", "LIKELY_FLAKY", "FLAKE_TIME")
			for _, s := range resp.GetStats() {
				d := s.GetData()
				ft := fmtDurationUsec(d.GetTotalFlakeRuntimeUsec())
				t.row(s.GetLabel(),
					fmt.Sprintf("%d", d.GetTotalRuns()),
					fmt.Sprintf("%d", d.GetSuccessfulRuns()),
					fmt.Sprintf("%d", d.GetFailedRuns()),
					fmt.Sprintf("%d", d.GetFlakyRuns()),
					fmt.Sprintf("%d", d.GetLikelyFlakyRuns()),
					ft)
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect)")
	return cmd
}

func targetFlakesSubCmd() *cobra.Command {
	var repo string
	cmd := &cobra.Command{
		Use:   "flakes <target-label>",
		Short: "Show flake samples for a target",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			if repo == "" {
				repo, err = detectRepoURL()
				if err != nil {
					return fmt.Errorf("auto-detect repo (use --repo to override): %w", err)
				}
			}
			groupID, err := c.resolveGroupID(repo)
			if err != nil {
				return err
			}
			req := &targetpb.GetTargetFlakeSamplesRequest{
				RequestContext: &ctxpb.RequestContext{GroupId: groupID},
				Label:          args[0],
				Repo:           repo,
			}
			resp := &targetpb.GetTargetFlakeSamplesResponse{}
			if err := c.call("GetTargetFlakeSamples", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("STATUS", "STARTED", "INVOCATION")
			for _, s := range resp.GetSamples() {
				started := time.UnixMicro(s.GetInvocationStartTimeUsec()).Format("2006-01-02 15:04")
				t.row(s.GetStatus().String(), started, s.GetInvocationId())
			}
			t.flush()
			if len(resp.GetSamples()) == 0 {
				fmt.Println("No flake samples found")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect)")
	return cmd
}

func targetLogSubCmd() *cobra.Command {
	var artifactName string
	var all bool
	cmd := &cobra.Command{
		Use:   "log <invocation-id> <target-label-or-substring>",
		Short: "Print test.log for a target (downloads from BES artifacts)",
		Long: `Download and print the test log for a specific target.

Uses the BES (Build Event Stream) artifacts to find and download the test.log
for the given target. The second argument is matched as a substring against
"label/name" (e.g., "test_lifecycle" matches "//mcp_infra/compositor:test_lifecycle/test.log").

A target that is sharded, runs more than once or is retried has one log per shard, run and
attempt, so several can match. When they do, the logs of the results that did not pass are
printed, each under a header naming its shard and status; if none failed, the first is. Which
others exist, and how to read them, is listed on stderr. Pick results yourself with --shard
(numbered from 1, as in shard_N_of_M) and --failed, or print every match with --all.

Examples:
  bbapi target log <invocation-id> test_lifecycle
  bbapi target log <invocation-id> //mcp_infra/compositor:test_lifecycle
  bbapi target log <invocation-id> test_lifecycle --artifact test.xml
  bbapi target log <invocation-id> visual --shard 4     # one shard of a sharded test
  bbapi target log <invocation-id> visual --all         # every shard, with headers`,
		Args: cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			artifacts, err := listArtifactsResolved(c, args[0])
			if err != nil {
				return err
			}
			// Filter to matching target and artifact name. Test artifacts only:
			// a build output of a target named like a test must not shadow the
			// test log this command exists to print.
			var matches []artifact
			for _, a := range artifacts {
				if a.Kind != kindTest {
					continue
				}
				combined := a.Label + "/" + a.Name
				if strings.Contains(combined, args[1]) && strings.Contains(a.Name, artifactName) {
					matches = append(matches, a)
				}
			}
			if len(matches) == 0 {
				// Provide helpful suggestions
				fmt.Fprintf(os.Stderr, "No artifacts matching target %q with artifact %q\n", args[1], artifactName)
				if artifactShard != 0 || artifactFailed {
					fmt.Fprintf(os.Stderr, "(narrowed by --shard=%d --failed=%t)\n", artifactShard, artifactFailed)
				}
				fmt.Fprintln(os.Stderr)
				var targetMatches []artifact
				for _, a := range artifacts {
					if strings.Contains(a.Label+"/"+a.Name, args[1]) {
						targetMatches = append(targetMatches, a)
					}
				}
				if len(targetMatches) > 0 {
					fmt.Fprintf(os.Stderr, "Artifacts for matching targets:\n")
					for _, a := range targetMatches {
						fmt.Fprintf(os.Stderr, "  %s  %s\n", a.Label, a.Name)
					}
				} else {
					// Show a few available targets as hints
					seen := map[string]bool{}
					count := 0
					fmt.Fprintf(os.Stderr, "No targets match %q. Available targets (first 5):\n", args[1])
					for _, a := range artifacts {
						if !seen[a.Label] {
							seen[a.Label] = true
							fmt.Fprintf(os.Stderr, "  %s\n", a.Label)
							count++
							if count >= 5 {
								fmt.Fprintf(os.Stderr, "  ... (%d more)\n", len(artifacts)-count)
								break
							}
						}
					}
				}
				return fmt.Errorf("no matching artifacts found")
			}
			chosen := matches
			if len(matches) > 1 {
				fmt.Fprintf(os.Stderr, "%d logs match:\n", len(matches))
				for _, a := range matches {
					fmt.Fprintf(os.Stderr, "  %s\n", a.describe())
				}
				if !all {
					var why string
					chosen, why = chooseLogs(matches)
					fmt.Fprintf(os.Stderr, "%s (--shard N, --failed or --all picks others)\n", why)
				}
			}
			for i, a := range chosen {
				if len(chosen) > 1 {
					if i > 0 {
						fmt.Println()
					}
					fmt.Printf("==> %s <==\n", a.describe())
				}
				if err := printArtifact(c, a); err != nil {
					return err
				}
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&artifactName, "artifact", "test.log", "Artifact name to download (default: test.log)")
	cmd.Flags().BoolVar(&all, "all", false, "print every matching log, each under a header, instead of the failing ones")
	addResultFlags(cmd)
	return cmd
}

// chooseLogs picks which of several matching logs to print when the caller named none: those of results that
// did not pass, since a log is mostly read to see why something failed, and otherwise the first. It says
// which it did, and why.
func chooseLogs(matches []artifact) ([]artifact, string) {
	var failing []artifact
	for _, a := range matches {
		if a.failed() {
			failing = append(failing, a)
		}
	}
	if len(failing) > 0 {
		return failing, fmt.Sprintf("Printing the %d of %d that did not pass", len(failing), len(matches))
	}
	return matches[:1], "None failed; printing the first"
}

// parseSince parses a --since value as a Go duration (e.g., "168h") or date (YYYY-MM-DD).
// Returns the parsed time, or zero time if since is empty.
func parseSince(since string, now time.Time) (time.Time, error) {
	if since == "" {
		return time.Time{}, nil
	}
	if d, err := time.ParseDuration(since); err == nil {
		return now.Add(-d), nil
	}
	if t, err := time.Parse("2006-01-02", since); err == nil {
		return t, nil
	}
	return time.Time{}, fmt.Errorf("expected Go duration (168h, 24h) or date (YYYY-MM-DD), got %q", since)
}
