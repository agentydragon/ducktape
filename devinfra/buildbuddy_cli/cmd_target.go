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

			var allTargets []*targetpb.TargetHistory
			printed := 0
			for {
				resp := &targetpb.GetTargetHistoryResponse{}
				if err := c.call("GetTargetHistory", req, resp); err != nil {
					return err
				}
				if jsonOutput {
					return printProtoJSON(resp)
				}
				allTargets = append(allTargets, resp.GetInvocationTargets()...)
				if resp.GetNextPageToken() == "" {
					break
				}
				req.PageToken = resp.GetNextPageToken()
			}
			for _, th := range allTargets {
				if label != "" && th.GetTarget().GetLabel() != label {
					continue
				}
				// Filter statuses by --since and --failures-only
				var filtered []*targetpb.TargetStatus
				for _, s := range th.GetTargetStatus() {
					if failuresOnly && s.GetStatus().String() == "PASSED" {
						continue
					}
					// Use invocation creation time, not test start time (cached tests report original start)
					invTime := time.UnixMicro(s.GetInvocationCreatedAtUsec())
					if !sinceTime.IsZero() && invTime.Before(sinceTime) {
						continue
					}
					filtered = append(filtered, s)
				}
				if failuresOnly && len(filtered) == 0 {
					continue
				}
				fmt.Printf("Target: %s\n", th.GetTarget().GetLabel())
				t := newTable()
				t.header("STATUS", "DUR", "STARTED", "COMMIT", "INVOCATION")
				for _, s := range filtered {
					started := s.GetTiming().GetStartTime().AsTime().Format("2006-01-02 15:04")
					dur := fmtDurationUsec(s.GetTiming().GetDuration().AsDuration().Microseconds())
					sha := s.GetCommitSha()
					if len(sha) > 8 {
						sha = sha[:8]
					}
					t.row(s.GetStatus().String(), dur, started, sha, s.GetInvocationId())
				}
				t.flush()
				printed++
				if count > 0 && printed >= count {
					break
				}
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
	cmd := &cobra.Command{
		Use:   "log <invocation-id> <target-label-or-substring>",
		Short: "Print test.log for a target (downloads from BES artifacts)",
		Long: `Download and print the test log for a specific target.

Uses the BES (Build Event Stream) artifacts to find and download the test.log
for the given target. The second argument is matched as a substring against
"label/name" (e.g., "test_handlers" matches "//x/gatelet/server/auth:test_handlers/test.log").

Examples:
  bbapi target log <invocation-id> test_handlers
  bbapi target log <invocation-id> //x/gatelet/server/auth:test_handlers
  bbapi target log <invocation-id> test_handlers --artifact test.xml`,
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
			// Filter to matching target and artifact name
			var matches []artifact
			for _, a := range artifacts {
				combined := a.Label + "/" + a.Name
				if strings.Contains(combined, args[1]) && strings.Contains(a.Name, artifactName) {
					matches = append(matches, a)
				}
			}
			if len(matches) == 0 {
				// Provide helpful suggestions
				fmt.Fprintf(os.Stderr, "No artifacts matching target %q with artifact %q\n\n", args[1], artifactName)
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
			if len(matches) > 1 {
				fmt.Fprintf(os.Stderr, "Multiple matches, using first:\n")
				for _, a := range matches {
					fmt.Fprintf(os.Stderr, "  %s  %s\n", a.Label, a.Name)
				}
			}
			return catArtifact(c, matches[:1], matches[0].Label+"/"+matches[0].Name)
		},
	}
	cmd.Flags().StringVar(&artifactName, "artifact", "test.log", "Artifact name to download (default: test.log)")
	return cmd
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
