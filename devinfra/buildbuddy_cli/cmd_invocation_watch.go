package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"time"

	commonpb "github.com/buildbuddy-io/buildbuddy/proto/api/v1/common"
	ctxpb "github.com/buildbuddy-io/buildbuddy/proto/context"
	eventlogpb "github.com/buildbuddy-io/buildbuddy/proto/eventlog"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	invocationstatuspb "github.com/buildbuddy-io/buildbuddy/proto/invocation_status"
	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"github.com/spf13/cobra"
)

func invocationWatchCmd() *cobra.Command {
	var pollInterval time.Duration
	var failOnTestFailure bool
	cmd := &cobra.Command{
		Use:   "watch <invocation-id>",
		Short: "Stream live BuildBuddy logs for an invocation",
		Long: `Follow the live BuildBuddy build log while an invocation is running.

The log comes from BuildBuddy's event log, which is written from the Build Event
Stream. Use --fail-on-test-failure to exit as soon as BuildBuddy indexes a
non-passing test result, even if Bazel is still running other tests.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			return watchInvocation(c, args[0], os.Stdout, pollInterval, failOnTestFailure)
		},
	}
	cmd.Flags().DurationVar(&pollInterval, "poll-interval", 2*time.Second, "How often to check for new log chunks")
	cmd.Flags().BoolVar(&failOnTestFailure, "fail-on-test-failure", false, "Exit when the first non-passing test result is indexed")
	return cmd
}

func getInvocation(c *client, invocationID string) (*invocationpb.Invocation, error) {
	req := &invocationpb.GetInvocationRequest{
		Lookup: &invocationpb.InvocationLookup{InvocationId: invocationID},
	}
	resp := &invocationpb.GetInvocationResponse{}
	if err := c.call("GetInvocation", req, resp); err != nil {
		return nil, err
	}
	if len(resp.GetInvocation()) == 0 {
		return nil, fmt.Errorf("BuildBuddy invocation %s was not found", invocationID)
	}
	return resp.GetInvocation()[0], nil
}

func failedTest(c *client, invocationID, groupID string) (string, error) {
	status := commonpb.Status_FAILED
	req := &targetpb.GetTargetRequest{
		RequestContext: &ctxpb.RequestContext{GroupId: groupID},
		InvocationId:   invocationID,
		Status:         &status,
	}
	resp := &targetpb.GetTargetResponse{}
	if err := c.call("GetTarget", req, resp); err != nil {
		return "", err
	}
	for _, group := range resp.GetTargetGroups() {
		for _, target := range group.GetTargets() {
			if target.GetMetadata().GetLabel() != "" {
				return target.GetMetadata().GetLabel(), nil
			}
		}
	}
	return "", nil
}

func watchInvocation(c *client, invocationID string, out io.Writer, pollInterval time.Duration, failFast bool) error {
	if pollInterval <= 0 {
		return fmt.Errorf("poll interval must be positive")
	}
	inv, err := getInvocation(c, invocationID)
	if err != nil {
		return err
	}
	groupID := inv.GetAcl().GetGroupId()
	if groupID == "" {
		return fmt.Errorf("BuildBuddy did not return a group ID for invocation %s", invocationID)
	}
	fmt.Fprintf(out, "Watching BuildBuddy invocation %s (https://app.buildbuddy.io/invocation/%s)\n", invocationID, invocationID)

	chunkID := inv.GetLastChunkId()
	lastChunkBuffer := []byte(nil)
	consoleOffset := 0
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		inv, err = getInvocation(c, invocationID)
		if err != nil {
			return err
		}
		if chunkID == "" {
			chunkID = inv.GetLastChunkId()
		}
		if chunkID != "" {
			chunk := &eventlogpb.GetEventLogChunkResponse{}
			request := &eventlogpb.GetEventLogChunkRequest{
				RequestContext: &ctxpb.RequestContext{GroupId: groupID},
				InvocationId:   invocationID,
				ChunkId:        chunkID,
				Type:           eventlogpb.LogType_BUILD_LOG,
			}
			if err := c.call("GetEventLogChunk", request, chunk); err != nil {
				return fmt.Errorf("read BuildBuddy event log: %w", err)
			}
			buffer := chunk.GetBuffer()
			if !bytes.HasPrefix(buffer, lastChunkBuffer) {
				lastChunkBuffer = nil
			}
			if len(buffer) > len(lastChunkBuffer) {
				if _, err := out.Write(buffer[len(lastChunkBuffer):]); err != nil {
					return fmt.Errorf("write BuildBuddy log: %w", err)
				}
			}
			lastChunkBuffer = append(lastChunkBuffer[:0], buffer...)
			if chunk.GetNextChunkId() != "" {
				chunkID = chunk.GetNextChunkId()
				lastChunkBuffer = nil
				if failFast {
					label, err := failedTest(c, invocationID, groupID)
					if err != nil {
						return fmt.Errorf("check test results: %w", err)
					}
					if label != "" {
						return fmt.Errorf("first non-passing test result: %s", label)
					}
				}
				continue
			}
		} else if !inv.GetHasChunkedEventLogs() {
			console := []byte(inv.GetConsoleBuffer())
			if len(console) > consoleOffset {
				if _, err := out.Write(console[consoleOffset:]); err != nil {
					return fmt.Errorf("write BuildBuddy console log: %w", err)
				}
				consoleOffset = len(console)
			}
		}

		if failFast {
			label, err := failedTest(c, invocationID, groupID)
			if err != nil {
				return fmt.Errorf("check test results: %w", err)
			}
			if label != "" {
				return fmt.Errorf("first non-passing test result: %s", label)
			}
		}

		switch inv.GetInvocationStatus() {
		case invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS:
			return nil
		case invocationstatuspb.InvocationStatus_DISCONNECTED_INVOCATION_STATUS:
			return fmt.Errorf("BuildBuddy invocation %s disconnected before completion", invocationID)
		}
		<-ticker.C
	}
}
