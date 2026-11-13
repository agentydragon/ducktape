package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"

	repb "github.com/buildbuddy-io/buildbuddy/proto/remote_execution"
	"github.com/spf13/cobra"
	"google.golang.org/grpc/metadata"
)

// These are action observations, not invocation or CI gate verdicts. Keep zero
// status/exit values explicit when present and absent while still in progress.
type executionWaitUpdate struct {
	ExecutionID   string `json:"execution_id"`
	Stage         string `json:"stage"`
	Done          bool   `json:"done"`
	ResultPresent bool   `json:"result_present"`
	StatusCode    *int32 `json:"status_code,omitempty"`
	StatusMessage string `json:"status_message,omitempty"`
	ExitCode      *int32 `json:"exit_code,omitempty"`
}

func executionWaitCmd() *cobra.Command {
	var target string
	cmd := &cobra.Command{
		Use:   "wait <execution-id>",
		Short: "Observe an accepted action with read-only WaitExecution",
		Long: `Attach to the exact existing execution ID without submitting or cancelling work.
Print updates until a terminal action result arrives. With --json, each update
is one JSON line with explicit terminal status/exit values. A zero exit means
this action returned a result with OK status and exit code zero; it says nothing
about other actions, the Bazel invocation or the native CI job. A stream failure,
missing result, nonzero status or nonzero exit code returns an error. Re-run this
command with the same exact ID to reconnect; it never falls back to Execute.
Use bb execution get <exact-id> for a retained cached response after completion.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			key := os.Getenv("BUILDBUDDY_API_KEY")
			if key == "" {
				return fmt.Errorf("BUILDBUDDY_API_KEY environment variable is not set")
			}
			conn, err := newRPCConnection(target, key, requestTimeout, 0)
			if err != nil {
				return err
			}
			defer conn.Close()
			ctx := cmd.Context()
			if requestTimeout > 0 {
				var cancel context.CancelFunc
				ctx, cancel = context.WithTimeout(ctx, requestTimeout)
				defer cancel()
			}
			// The existing interceptor authenticates unary calls only.
			ctx = metadata.AppendToOutgoingContext(ctx, "x-buildbuddy-api-key", key)
			return waitForExecution(ctx, repb.NewExecutionClient(conn), args[0], func(update executionWaitUpdate) error {
				if jsonOutput {
					return json.NewEncoder(cmd.OutOrStdout()).Encode(update)
				}
				if !update.Done {
					_, err := fmt.Fprintf(cmd.OutOrStdout(), "%s %s (action pending)\n", update.ExecutionID, update.Stage)
					return err
				}
				_, err := fmt.Fprintf(cmd.OutOrStdout(), "%s %s status=%v result_present=%t exit_code=%v\n",
					update.ExecutionID, update.Stage, optionalInt32(update.StatusCode), update.ResultPresent, optionalInt32(update.ExitCode))
				return err
			})
		},
	}
	cmd.Flags().StringVar(&target, "target", "https://remote.buildbuddy.io", "Execution service URL (https:// or http://)")
	return cmd
}

func optionalInt32(value *int32) any {
	if value == nil {
		return "unknown"
	}
	return *value
}

func waitForExecution(ctx context.Context, client repb.ExecutionClient, executionID string, emit func(executionWaitUpdate) error) error {
	stream, err := client.WaitExecution(ctx, &repb.WaitExecutionRequest{Name: executionID})
	if err != nil {
		return fmt.Errorf("attach to accepted action: %w", err)
	}
	for {
		op, err := stream.Recv()
		if err == io.EOF {
			return fmt.Errorf("execution stream ended without a terminal action result")
		}
		if err != nil {
			return fmt.Errorf("observe accepted action: %w", err)
		}
		if op.GetName() != executionID {
			return fmt.Errorf("execution identity mismatch: requested %q, received %q", executionID, op.GetName())
		}
		md := &repb.ExecuteOperationMetadata{}
		if op.GetMetadata() == nil && !op.GetDone() {
			return fmt.Errorf("execution update has no metadata")
		}
		if op.GetMetadata() != nil {
			if err := op.GetMetadata().UnmarshalTo(md); err != nil {
				return fmt.Errorf("decode execution metadata: %w", err)
			}
		}
		update := executionWaitUpdate{ExecutionID: executionID, Stage: md.GetStage().String(), Done: op.GetDone()}
		var terminalErr error
		if op.GetDone() {
			if failure := op.GetError(); failure != nil {
				code := failure.GetCode()
				update.StatusCode, update.StatusMessage = &code, failure.GetMessage()
				terminalErr = fmt.Errorf("action operation failed: status=%d %s", code, failure.GetMessage())
			} else if op.GetResponse() == nil {
				terminalErr = fmt.Errorf("terminal action operation has no ExecuteResponse")
			} else {
				response := &repb.ExecuteResponse{}
				if err := op.GetResponse().UnmarshalTo(response); err != nil {
					return fmt.Errorf("decode ExecuteResponse: %w", err)
				}
				code := response.GetStatus().GetCode()
				update.StatusCode, update.StatusMessage = &code, response.GetStatus().GetMessage()
				update.ResultPresent = response.GetResult() != nil
				if update.ResultPresent {
					exitCode := response.GetResult().GetExitCode()
					update.ExitCode = &exitCode
				}
				switch {
				case code != 0:
					terminalErr = fmt.Errorf("action execution failed: status=%d %s", code, update.StatusMessage)
				case !update.ResultPresent:
					terminalErr = fmt.Errorf("terminal ExecuteResponse has no action result")
				case *update.ExitCode != 0:
					terminalErr = fmt.Errorf("action process exited with code %d", *update.ExitCode)
				}
			}
		}
		if err := emit(update); err != nil {
			return err
		}
		if op.GetDone() {
			return terminalErr
		}
	}
}
