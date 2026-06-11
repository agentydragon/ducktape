package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	ctxpb "github.com/buildbuddy-io/buildbuddy/proto/context"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"github.com/spf13/cobra"
)

func formatTags(inv *invocationpb.Invocation) string {
	var names []string
	for _, t := range inv.GetTags() {
		names = append(names, t.GetName())
	}
	return strings.Join(names, ",")
}

func invocationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "invocation <invocation-id>",
		Short: "Show invocation details (default), or use subcommands",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			req := &invocationpb.GetInvocationRequest{
				Lookup: &invocationpb.InvocationLookup{
					InvocationId: args[0],
				},
			}
			resp := &invocationpb.GetInvocationResponse{}
			if err := c.call("GetInvocation", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			for _, inv := range resp.GetInvocation() {
				sha := inv.GetCommitSha()
				if len(sha) > 8 {
					sha = sha[:8]
				}
				fmt.Printf("Invocation:  %s\n", inv.GetInvocationId())
				fmt.Printf("Status:      %s\n", inv.GetInvocationStatus().String())
				fmt.Printf("Command:     %s %s\n", inv.GetCommand(), strings.Join(inv.GetPattern(), " "))
				fmt.Printf("Created:     %s\n", time.UnixMicro(inv.GetCreatedAtUsec()).Format(time.RFC3339))
				fmt.Printf("Duration:    %s\n", fmtDurationUsec(inv.GetDurationUsec()))
				fmt.Printf("User:        %s\n", inv.GetUser())
				fmt.Printf("Host:        %s\n", inv.GetHost())
				fmt.Printf("Commit:      %s\n", sha)
				fmt.Printf("Branch:      %s\n", inv.GetBranchName())
				fmt.Printf("Repo:        %s\n", inv.GetRepoUrl())
				if role := inv.GetRole(); role != "" {
					fmt.Printf("Role:        %s\n", role)
				}
				if tags := formatTags(inv); tags != "" {
					fmt.Printf("Tags:        %s\n", tags)
				}
				fmt.Printf("Actions:     %d\n", inv.GetActionCount())
				fmt.Printf("Success:     %v\n", inv.GetSuccess())
				if cs := inv.GetCacheStats(); cs != nil {
					fmt.Printf("AC Hits:     %d\n", cs.GetActionCacheHits())
					fmt.Printf("AC Misses:   %d\n", cs.GetActionCacheMisses())
					fmt.Printf("CAS Hits:    %d\n", cs.GetCasCacheHits())
					fmt.Printf("DL bytes:    %d\n", cs.GetTotalDownloadSizeBytes())
					fmt.Printf("UL bytes:    %d\n", cs.GetTotalUploadSizeBytes())
				}
				// Show child invocation IDs (for workflow invocations)
				for _, ev := range inv.GetEvent() {
					be := ev.GetBuildEvent()
					if be == nil {
						continue
					}
					for _, child := range be.GetChildren() {
						if cid := child.GetChildInvocationCompleted(); cid != nil && cid.GetInvocationId() != "" {
							fmt.Printf("Child:       %s\n", cid.GetInvocationId())
						}
					}
				}
			}
			return nil
		},
	}
	cmd.AddCommand(invocationListCmd())
	cmd.AddCommand(invocationStatSubCmd())
	return cmd
}

func invocationListCmd() *cobra.Command {
	var repo string
	var count int32
	var tagFilter string
	var roleFilter string
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List recent invocations",
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
			query := &invocationpb.InvocationQuery{RepoUrl: repo}
			if tagFilter != "" {
				query.Tags = []string{tagFilter}
			}
			if roleFilter != "" {
				query.Role = []string{roleFilter}
			}
			req := &invocationpb.SearchInvocationRequest{
				Query: query,
				Count: count,
			}
			resp := &invocationpb.SearchInvocationResponse{}
			if err := c.call("SearchInvocation", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("INVOCATION", "CREATED", "DUR", "COMMAND", "STATUS", "ROLE", "TAGS", "SHA")
			for _, inv := range resp.GetInvocation() {
				sha := inv.GetCommitSha()
				if len(sha) > 8 {
					sha = sha[:8]
				}
				t.row(
					inv.GetInvocationId(),
					time.UnixMicro(inv.GetCreatedAtUsec()).Format("2006-01-02 15:04"),
					fmtDurationUsec(inv.GetDurationUsec()),
					inv.GetCommand()+" "+strings.Join(inv.GetPattern(), " "),
					inv.GetInvocationStatus().String(),
					inv.GetRole(),
					formatTags(inv),
					sha,
				)
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().Int32Var(&count, "count", 10, "Number of invocations to list")
	cmd.Flags().StringVar(&tagFilter, "tag", "", "Filter by tag (exact match)")
	cmd.Flags().StringVar(&roleFilter, "role", "", "Filter by role (exact match)")
	return cmd
}

// getChildInvocationIDs returns child invocation IDs for a workflow invocation.
// Returns nil if the invocation has no children (i.e., is not a workflow wrapper).
func getChildInvocationIDs(c *client, invocationID string) ([]string, error) {
	req := &invocationpb.GetInvocationRequest{
		Lookup: &invocationpb.InvocationLookup{InvocationId: invocationID},
	}
	resp := &invocationpb.GetInvocationResponse{}
	if err := c.call("GetInvocation", req, resp); err != nil {
		return nil, err
	}
	var children []string
	for _, inv := range resp.GetInvocation() {
		for _, ev := range inv.GetEvent() {
			be := ev.GetBuildEvent()
			if be == nil {
				continue
			}
			for _, child := range be.GetChildren() {
				if cid := child.GetChildInvocationCompleted(); cid != nil && cid.GetInvocationId() != "" {
					children = append(children, cid.GetInvocationId())
				}
			}
		}
	}
	return children, nil
}

// resolveInvocationIDs returns the invocation ID(s) to query for artifacts/targets.
// For workflow invocations (which have children), returns child IDs.
// For regular invocations, returns the original ID.
func resolveInvocationIDs(c *client, invocationID string) ([]string, error) {
	children, err := getChildInvocationIDs(c, invocationID)
	if err != nil {
		return nil, err
	}
	if len(children) > 0 {
		fmt.Fprintf(os.Stderr, "Workflow invocation, using child: %s\n", strings.Join(children, ", "))
		return children, nil
	}
	return []string{invocationID}, nil
}

var aggTypeMap = map[string]invocationpb.AggType{
	"user":    invocationpb.AggType_USER_AGGREGATION_TYPE,
	"host":    invocationpb.AggType_HOSTNAME_AGGREGATION_TYPE,
	"repo":    invocationpb.AggType_REPO_URL_AGGREGATION_TYPE,
	"commit":  invocationpb.AggType_COMMIT_SHA_AGGREGATION_TYPE,
	"date":    invocationpb.AggType_DATE_AGGREGATION_TYPE,
	"branch":  invocationpb.AggType_BRANCH_AGGREGATION_TYPE,
	"pattern": invocationpb.AggType_PATTERN_AGGREGATION_TYPE,
}

// TODO: --agg-type date returns a server-side ClickHouse error:
// "Illegal type Float64 of first argument of function fromUnixTimestamp".
// BuildBuddy stores updated_at_usec as Float64 but FROM_UNIXTIME expects integer.
// Other aggregation types (branch, user, host, etc.) work fine.

func invocationStatSubCmd() *cobra.Command {
	var repo string
	var aggType string
	var limit int32
	cmd := &cobra.Command{
		Use:   "stat",
		Short: "Show aggregated invocation statistics",
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
			at, ok := aggTypeMap[aggType]
			if !ok {
				return fmt.Errorf("unknown --agg-type %q; valid: user, host, repo, commit, date, branch, pattern", aggType)
			}
			groupID, err := c.resolveGroupID(repo)
			if err != nil {
				return err
			}
			req := &invocationpb.GetInvocationStatRequest{
				RequestContext:  &ctxpb.RequestContext{GroupId: groupID},
				AggregationType: at,
				Limit:           limit,
				Query: &invocationpb.InvocationStatQuery{
					RepoUrl: repo,
				},
			}
			resp := &invocationpb.GetInvocationStatResponse{}
			if err := c.call("GetInvocationStat", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("NAME", "BUILDS", "PASS", "FAIL", "ACTIONS", "BUILD_TIME", "LAST_GREEN", "LAST_RED")
			for _, s := range resp.GetInvocationStat() {
				lastGreen := "-"
				if s.GetLastGreenBuildUsec() > 0 {
					lastGreen = time.UnixMicro(s.GetLastGreenBuildUsec()).Format("2006-01-02 15:04")
				}
				lastRed := "-"
				if s.GetLastRedBuildUsec() > 0 {
					lastRed = time.UnixMicro(s.GetLastRedBuildUsec()).Format("2006-01-02 15:04")
				}
				t.row(
					s.GetName(),
					s.GetTotalNumBuilds(),
					s.GetTotalNumSucessfulBuilds(),
					s.GetTotalNumFailingBuilds(),
					s.GetTotalActions(),
					fmtDurationUsec(s.GetTotalBuildTimeUsec()),
					lastGreen,
					lastRed,
				)
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().StringVar(&aggType, "agg-type", "branch", "Aggregation type: user, host, repo, commit, date (broken), branch, pattern")
	cmd.Flags().Int32Var(&limit, "limit", 20, "Maximum number of results")
	return cmd
}
