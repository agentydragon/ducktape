package main

import (
	"context"
	"fmt"

	"github.com/dustin/go-humanize"
	"github.com/spf13/cobra"

	cachepb "github.com/buildbuddy-io/buildbuddy/proto/cache"
	executionpb "github.com/buildbuddy-io/buildbuddy/proto/execution_stats"
)

func executionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "execution <invocation-id>",
		Short: "List executions for an invocation (default), or use subcommands",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			req := &executionpb.GetExecutionRequest{
				ExecutionLookup: &executionpb.ExecutionLookup{
					InvocationId: args[0],
				},
			}
			resp, err := c.rpc.GetExecution(context.Background(), req)
			if err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("EXECUTION", "STAGE", "STATUS")
			for _, ex := range resp.GetExecution() {
				status := "OK"
				if s := ex.GetStatus(); s != nil && s.GetCode() != 0 {
					status = fmt.Sprintf("code=%d %s", s.GetCode(), s.GetMessage())
				}
				t.row(ex.GetExecutionId(), ex.GetStage().String(), status)
			}
			t.flush()
			return nil
		},
	}
	cmd.AddCommand(executionSearchCmd())
	cmd.AddCommand(executionFilesCmd())
	return cmd
}

func executionSearchCmd() *cobra.Command {
	var repo string
	var count int32
	cmd := &cobra.Command{
		Use:   "search",
		Short: "Search remote executions across invocations",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			if repo == "" {
				repo, err = detectRepoURL()
				if err != nil {
					return fmt.Errorf("auto-detect repo (use --repo to override): %w", err)
				}
			}
			req := &executionpb.SearchExecutionRequest{
				Query: &executionpb.ExecutionQuery{
					RepoUrl: repo,
				},
				Count: count,
			}
			resp, err := c.rpc.SearchExecution(context.Background(), req)
			if err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("INVOCATION", "EXECUTION", "STAGE", "STATUS", "PATTERN")
			for _, ewm := range resp.GetExecution() {
				ex := ewm.GetExecution()
				meta := ewm.GetInvocationMetadata()
				status := "OK"
				if s := ex.GetStatus(); s != nil && s.GetCode() != 0 {
					status = fmt.Sprintf("code=%d %s", s.GetCode(), s.GetMessage())
				}
				t.row(meta.GetId(), ex.GetExecutionId(), ex.GetStage().String(), status, meta.GetPattern())
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().StringVar(&repo, "repo", "", "Repository URL (default: auto-detect from git)")
	cmd.Flags().Int32Var(&count, "count", 20, "Number of results to return")
	return cmd
}

func executionFilesCmd() *cobra.Command {
	var pageSize int32
	cmd := &cobra.Command{
		Use:   "files <invocation-id> <execution-id>",
		Short: "List a remote execution's GetExecutionDownloads file records",
		Long: "List the file records returned by GetExecutionDownloads. This does not enumerate " +
			"the action's complete input-root tree. The command fails if the API returns more " +
			"than the 100-page safety limit. The execution ID is the one shown by " +
			"`bbapi execution <invocation-id>`.",
		Args: cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			out := &cachepb.GetExecutionDownloadsResponse{}
			var pageToken string
			for i := 0; i < 100; i++ { // bound pagination as a safety cap
				req := &cachepb.GetExecutionDownloadsRequest{
					InvocationId: args[0],
					ExecutionId:  args[1],
					PageSize:     pageSize,
					PageToken:    pageToken,
				}
				resp, err := c.rpc.GetExecutionDownloads(context.Background(), req)
				if err != nil {
					return err
				}
				out.Downloads = append(out.Downloads, resp.GetDownloads()...)
				pageToken = resp.GetNextPageToken()
				if pageToken == "" {
					break
				}
			}
			if pageToken != "" {
				return fmt.Errorf(
					"execution download listing exceeded the 100-page safety limit; results are incomplete",
				)
			}
			if jsonOutput {
				return printProtoJSON(out)
			}
			t := newTable()
			t.header("PATH", "SIZE", "EXEC", "DIGEST")
			for _, dl := range out.GetDownloads() {
				exec := ""
				if dl.GetIsExecutable() {
					exec = "x"
				}
				dg := dl.GetDigest()
				t.row(dl.GetPath(), humanize.IBytes(uint64(dg.GetSizeBytes())), exec, dg.GetHash())
			}
			t.flush()
			return nil
		},
	}
	cmd.Flags().Int32Var(&pageSize, "page-size", 0, "Results per page (0 = server default)")
	return cmd
}
