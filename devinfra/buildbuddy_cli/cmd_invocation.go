package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	eventlogpb "github.com/buildbuddy-io/buildbuddy/proto/eventlog"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"github.com/spf13/cobra"
)

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
	cmd.AddCommand(invocationLogCmd())
	return cmd
}

func invocationListCmd() *cobra.Command {
	var repo string
	var count int32
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
			req := &invocationpb.SearchInvocationRequest{
				Query: &invocationpb.InvocationQuery{RepoUrl: repo},
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
			t.header("INVOCATION", "CREATED", "DUR", "COMMAND", "STATUS", "SHA")
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
					sha,
				)
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().Int32Var(&count, "count", 10, "Number of invocations to list")
	return cmd
}

func invocationLogCmd() *cobra.Command {
	var minLines int32
	cmd := &cobra.Command{
		Use:   "log <invocation-id>",
		Short: "Print build log",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			req := &eventlogpb.GetEventLogChunkRequest{
				InvocationId: args[0],
				MinLines:     minLines,
			}
			resp := &eventlogpb.GetEventLogChunkResponse{}
			if err := c.call("GetEventLogChunk", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			os.Stdout.Write(resp.GetBuffer())
			return nil
		},
	}
	cmd.Flags().Int32Var(&minLines, "lines", 500, "Minimum lines to fetch")
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
