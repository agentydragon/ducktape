package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
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
			req := &targetpb.GetTargetRequest{
				InvocationId: args[0],
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
			t := newTable()
			t.header("STATUS", "DUR", "RULE", "LABEL")
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
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&label, "label", "", "Filter to specific target label")
	cmd.Flags().StringVar(&filter, "filter", "", "Substring filter on target labels")
	cmd.AddCommand(targetHistorySubCmd())
	cmd.AddCommand(targetLogSubCmd())
	return cmd
}

func targetHistorySubCmd() *cobra.Command {
	var repo string
	var label string
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
			req := &targetpb.GetTargetHistoryRequest{
				Query: &targetpb.TargetQuery{
					RepoUrl: repo,
				},
				ServerSidePagination: true,
			}
			resp := &targetpb.GetTargetHistoryResponse{}
			if err := c.call("GetTargetHistory", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			for _, th := range resp.GetInvocationTargets() {
				if label != "" && th.GetTarget().GetLabel() != label {
					continue
				}
				fmt.Printf("Target: %s\n", th.GetTarget().GetLabel())
				t := newTable()
				t.header("STATUS", "DUR", "STARTED", "INVOCATION")
				for _, s := range th.GetTargetStatus() {
					started := s.GetTiming().GetStartTime().AsTime().Format("2006-01-02 15:04")
					dur := fmtDurationUsec(s.GetTiming().GetDuration().AsDuration().Microseconds())
					t.row(s.GetStatus().String(), dur, started, s.GetInvocationId())
				}
				t.flush()
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().StringVar(&label, "label", "", "Filter to specific target label")
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
			return downloadArtifact(c, matches[:1], matches[0].Label+"/"+matches[0].Name)
		},
	}
	cmd.Flags().StringVar(&artifactName, "artifact", "test.log", "Artifact name to download (default: test.log)")
	return cmd
}
