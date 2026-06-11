package main

import (
	"fmt"

	ctxpb "github.com/buildbuddy-io/buildbuddy/proto/context"
	workflowpb "github.com/buildbuddy-io/buildbuddy/proto/workflow"
	"github.com/spf13/cobra"
)

// For repos using buildbuddy.yaml + GitHub app, GetWorkflows returns empty.
// The workflow ID is synthetic: "WF#GitRepository:{group_id}:{repo_url}"
// (discoverable from workflow invocation metadata). The run subcommand
// auto-constructs this ID from the group_id and repo URL.

func workflowCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "workflow",
		Short: "List workflows (default), or use subcommands",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			req := &workflowpb.GetWorkflowsRequest{}
			resp := &workflowpb.GetWorkflowsResponse{}
			if err := c.call("GetWorkflows", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			t := newTable()
			t.header("ID", "NAME", "REPO")
			for _, w := range resp.GetWorkflow() {
				t.row(w.GetId(), w.GetName(), w.GetRepoUrl())
			}
			t.flush()
			return nil
		},
	}
	cmd.AddCommand(workflowRunSubCmd())
	return cmd
}

func workflowRunSubCmd() *cobra.Command {
	var workflowID string
	var branch string
	var commit string
	var actions []string
	var async bool
	cmd := &cobra.Command{
		Use:   "run",
		Short: "Trigger a workflow execution",
		RunE: func(_ *cobra.Command, _ []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			repo, err := detectRepoURL()
			if err != nil {
				return fmt.Errorf("auto-detect repo: %w", err)
			}
			groupID, err := c.resolveGroupID(repo)
			if err != nil {
				return err
			}
			// Auto-construct workflow ID from group_id + repo URL.
			// BuildBuddy uses synthetic IDs: "WF#GitRepository:{group_id}:{repo_url}"
			if workflowID == "" {
				workflowID = fmt.Sprintf("WF#GitRepository:%s:%s", groupID, repo)
			}
			if branch == "" || commit == "" {
				detectedBranch, detectedCommit, err := detectHead()
				if err != nil {
					return err
				}
				if branch == "" {
					branch = detectedBranch
				}
				if commit == "" {
					commit = detectedCommit
				}
			}
			req := &workflowpb.ExecuteWorkflowRequest{
				RequestContext: &ctxpb.RequestContext{GroupId: groupID},
				WorkflowId:     workflowID,
				CommitSha:      commit,
				PushedRepoUrl:  repo,
				PushedBranch:   branch,
				TargetRepoUrl:  repo,
				TargetBranch:   branch,
				ActionNames:    actions,
				Async:          async,
			}
			resp := &workflowpb.ExecuteWorkflowResponse{}
			if err := c.call("ExecuteWorkflow", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			for _, s := range resp.GetActionStatuses() {
				status := "OK"
				if st := s.GetStatus(); st != nil && st.GetCode() != 0 {
					status = fmt.Sprintf("code=%d %s", st.GetCode(), st.GetMessage())
				}
				fmt.Printf("%s  invocation=%s  status=%s\n", s.GetActionName(), s.GetInvocationId(), status)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&workflowID, "workflow-id", "", "Workflow ID (default: auto-detect from repo)")
	cmd.Flags().StringVar(&branch, "branch", "", "Branch name (default: current git branch)")
	cmd.Flags().StringVar(&commit, "commit", "", "Commit SHA (default: current git HEAD)")
	cmd.Flags().StringSliceVar(&actions, "action", nil, "Action name(s) to execute (repeatable)")
	cmd.Flags().BoolVar(&async, "async", false, "Fire-and-forget (don't wait for completion)")
	return cmd
}
