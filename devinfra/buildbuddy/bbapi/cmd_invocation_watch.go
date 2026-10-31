package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	commonpb "github.com/buildbuddy-io/buildbuddy/proto/api/v1/common"
	ctxpb "github.com/buildbuddy-io/buildbuddy/proto/context"
	eventlogpb "github.com/buildbuddy-io/buildbuddy/proto/eventlog"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	invocationstatuspb "github.com/buildbuddy-io/buildbuddy/proto/invocation_status"
	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"github.com/spf13/cobra"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func invocationWatchCmd() *cobra.Command {
	var pollInterval time.Duration
	var failOnTestFailure bool
	var resultFile string
	cmd := &cobra.Command{
		Use:   "watch <invocation-id>",
		Short: "Stream live BuildBuddy logs for an invocation",
		Long: `Follow the live BuildBuddy build log while an invocation is running.

The log comes from BuildBuddy's event log, which is written from the Build Event
Stream. Use --fail-on-test-failure to exit as soon as BuildBuddy indexes a
non-passing test result, even if Bazel is still running other tests. Use
--result-file to atomically write a versioned JSON outcome as soon as a terminal
invocation status is observed, while keeping the live log on stdout. Log
streaming continues after that status is recorded.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			defer c.close()
			return watchInvocation(c, args[0], os.Stdout, pollInterval, failOnTestFailure, resultFile)
		},
	}
	cmd.Flags().DurationVar(&pollInterval, "poll-interval", 2*time.Second, "How often to check for new log chunks")
	cmd.Flags().BoolVar(&failOnTestFailure, "fail-on-test-failure", false, "Exit when the first non-passing test result is indexed")
	cmd.Flags().StringVar(&resultFile, "result-file", "", "Write pending and final JSON watch states to this file")
	return cmd
}

// invocationWatchResult is the machine-readable outcome of an invocation watch.
// Version lets scripts reject an incompatible future schema explicitly.
type invocationWatchResult struct {
	Version            int    `json:"version"`
	InvocationID       string `json:"invocation_id"`
	URL                string `json:"url"`
	State              string `json:"state"`
	Reason             string `json:"reason"`
	InvocationComplete bool   `json:"invocation_complete"`
	FailedTest         string `json:"failed_test,omitempty"`
}

func writeInvocationWatchResult(path string, result invocationWatchResult) error {
	if path == "" {
		return nil
	}

	data, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal invocation watch result: %w", err)
	}
	data = append(data, '\n')

	temp, err := os.CreateTemp(filepath.Dir(path), ".bbapi-invocation-watch-*.json")
	if err != nil {
		return fmt.Errorf("create invocation watch result: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if _, err := temp.Write(data); err != nil {
		_ = temp.Close()
		return fmt.Errorf("write invocation watch result: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close invocation watch result: %w", err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("publish invocation watch result: %w", err)
	}
	return nil
}

func invocationWatchOutcome(c *client, invocationID, state, reason string, complete bool, failedTest string) invocationWatchResult {
	baseURL := strings.TrimRight(c.baseURL, "/")
	if baseURL == "" {
		baseURL = defaultBaseURL
	}
	return invocationWatchResult{
		Version:            1,
		InvocationID:       invocationID,
		URL:                fmt.Sprintf("%s/invocation/%s", baseURL, url.PathEscape(invocationID)),
		State:              state,
		Reason:             reason,
		InvocationComplete: complete,
		FailedTest:         failedTest,
	}
}

func terminalInvocationWatchOutcome(c *client, invocationID string, inv *invocationpb.Invocation) (invocationWatchResult, bool) {
	switch inv.GetInvocationStatus() {
	case invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS:
		state := "failure"
		if inv.GetSuccess() {
			state = "success"
		}
		return invocationWatchOutcome(c, invocationID, state, "completed", true, ""), true
	case invocationstatuspb.InvocationStatus_DISCONNECTED_INVOCATION_STATUS:
		return invocationWatchOutcome(c, invocationID, "error", "disconnected", false, ""), true
	default:
		return invocationWatchResult{}, false
	}
}

func recordInvocationWatchOutcome(resultFile string, c *client, invocationID, state, reason string, complete bool, failedTest string) error {
	return writeInvocationWatchResult(
		resultFile,
		invocationWatchOutcome(c, invocationID, state, reason, complete, failedTest),
	)
}

func getInvocation(c *client, invocationID string) (*invocationpb.Invocation, error) {
	req := &invocationpb.GetInvocationRequest{
		Lookup: &invocationpb.InvocationLookup{InvocationId: invocationID},
	}
	resp, err := c.rpc.GetInvocation(context.Background(), req)
	if err != nil {
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
	resp, err := c.rpc.GetTarget(context.Background(), req)
	if err != nil {
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

const firstBuildEventLogChunkID = "0000"

func isRecoverableEventLogReadError(err error) bool {
	switch status.Code(err) {
	case codes.ResourceExhausted, codes.Unavailable, codes.DeadlineExceeded:
		return true
	default:
		return false
	}
}

func watchInvocation(c *client, invocationID string, out io.Writer, pollInterval time.Duration, failFast bool, resultFile string) error {
	if pollInterval <= 0 {
		return fmt.Errorf("poll interval must be positive")
	}
	// Replace any previous result before the first API call so a failed lookup
	// cannot leave a stale terminal result for callers to mistake as current.
	if err := recordInvocationWatchOutcome(resultFile, c, invocationID, "pending", "watching", false, ""); err != nil {
		return err
	}
	inv, err := getInvocation(c, invocationID)
	if err != nil {
		return err
	}
	terminalOutcomeRecorded := false
	recordObservedOutcome := func(inv *invocationpb.Invocation) error {
		if terminalOutcomeRecorded {
			return nil
		}
		outcome, terminal := terminalInvocationWatchOutcome(c, invocationID, inv)
		if !terminal {
			return nil
		}
		if err := writeInvocationWatchResult(resultFile, outcome); err != nil {
			return err
		}
		terminalOutcomeRecorded = true
		return nil
	}
	if err := recordObservedOutcome(inv); err != nil {
		return err
	}
	groupID := inv.GetAcl().GetGroupId()
	if groupID == "" {
		return fmt.Errorf("BuildBuddy did not return a group ID for invocation %s", invocationID)
	}
	fmt.Fprintf(out, "Watching BuildBuddy invocation %s (https://app.buildbuddy.io/invocation/%s)\n", invocationID, invocationID)

	chunkID := inv.GetLastChunkId()
	logStartedFromFirstChunk := chunkID == firstBuildEventLogChunkID
	logBytesWritten := false
	logReadsDisabled := false
	var deferredLogErr error
	lastChunkBuffer := []byte(nil)
	consoleOffset := 0
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		inv, err = getInvocation(c, invocationID)
		if err != nil {
			return err
		}
		if err := recordObservedOutcome(inv); err != nil {
			return err
		}
		if chunkID == "" {
			chunkID = inv.GetLastChunkId()
			logStartedFromFirstChunk = chunkID == firstBuildEventLogChunkID
		}
		var chunk *eventlogpb.GetEventLogChunkResponse
		if chunkID != "" && !logReadsDisabled {
			request := &eventlogpb.GetEventLogChunkRequest{
				RequestContext: &ctxpb.RequestContext{GroupId: groupID},
				InvocationId:   invocationID,
				ChunkId:        chunkID,
				Type:           eventlogpb.LogType_BUILD_LOG,
			}
			chunk, err = c.rpc.GetEventLogChunk(context.Background(), request)
			if err != nil {
				logErr := fmt.Errorf("read BuildBuddy event log: %w", err)
				if isEventLogIndexLimitExceeded(err) && !logStartedFromFirstChunk && !logBytesWritten {
					// last_chunk_id is only a tail cursor. If the service cannot index
					// that cursor, restart at the first known chunk and follow the
					// response's next_chunk_id links instead of assuming a tail token
					// such as "ffff" has special meaning.
					chunkID = firstBuildEventLogChunkID
					logStartedFromFirstChunk = true
					lastChunkBuffer = nil
					continue
				}
				if !isRecoverableEventLogReadError(err) {
					return logErr
				}
				// Log reads have their own request timeout and bounded gRPC retries.
				// Stop retrying this stream after the read fails, but keep polling the
				// invocation so its terminal outcome is still recorded and reported.
				deferredLogErr = logErr
				logReadsDisabled = true
				lastChunkBuffer = nil
				chunk = nil
			}
		}
		if chunk != nil {
			buffer := chunk.GetBuffer()
			if !bytes.HasPrefix(buffer, lastChunkBuffer) {
				lastChunkBuffer = nil
			}
			if len(buffer) > len(lastChunkBuffer) {
				if _, err := out.Write(buffer[len(lastChunkBuffer):]); err != nil {
					return fmt.Errorf("write BuildBuddy log: %w", err)
				}
				logBytesWritten = true
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
						complete := inv.GetInvocationStatus() == invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS
						if err := recordInvocationWatchOutcome(resultFile, c, invocationID, "failure", "test_failure", complete, label); err != nil {
							return fmt.Errorf("first non-passing test result: %s (write result: %w)", label, err)
						}
						return fmt.Errorf("first non-passing test result: %s", label)
					}
				}
				continue
			}
		} else if chunkID == "" && !inv.GetHasChunkedEventLogs() {
			console := []byte(inv.GetConsoleBuffer())
			if len(console) > consoleOffset {
				if _, err := out.Write(console[consoleOffset:]); err != nil {
					return fmt.Errorf("write BuildBuddy console log: %w", err)
				}
				consoleOffset = len(console)
				logBytesWritten = true
			}
		}

		if failFast {
			label, err := failedTest(c, invocationID, groupID)
			if err != nil {
				return fmt.Errorf("check test results: %w", err)
			}
			if label != "" {
				complete := inv.GetInvocationStatus() == invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS
				if err := recordInvocationWatchOutcome(resultFile, c, invocationID, "failure", "test_failure", complete, label); err != nil {
					return fmt.Errorf("first non-passing test result: %s (write result: %w)", label, err)
				}
				return fmt.Errorf("first non-passing test result: %s", label)
			}
		}

		switch inv.GetInvocationStatus() {
		case invocationstatuspb.InvocationStatus_COMPLETE_INVOCATION_STATUS:
			if deferredLogErr != nil {
				return deferredLogErr
			}
			return nil
		case invocationstatuspb.InvocationStatus_DISCONNECTED_INVOCATION_STATUS:
			if deferredLogErr != nil {
				return fmt.Errorf("BuildBuddy invocation %s disconnected before completion; log streaming also failed: %w", invocationID, deferredLogErr)
			}
			return fmt.Errorf("BuildBuddy invocation %s disconnected before completion", invocationID)
		}
		<-ticker.C
	}
}
