package actions

import (
	"context"
	"errors"
	"io"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/actionstate"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func checkedReceipt(message *action_servicepb.ActionRequest) (*action_servicepb.ActionRequest, error) {
	if message == nil {
		return nil, invocation.InvalidResponse("response has no Action request")
	}
	if !actionstate.Valid(message.GetState()) {
		return nil, invocation.InvalidResponse("unrecognized Action state %q", message.GetState())
	}
	return message, nil
}

// waitUntil is a condition on a receipt, in the Action Service's own terms.
type waitUntil action_servicepb.WatchUntil

const (
	untilDecision waitUntil = waitUntil(action_servicepb.WatchUntil_WATCH_UNTIL_DECISION)
	untilTerminal waitUntil = waitUntil(action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL)
)

func parseUntil(value string) (waitUntil, error) {
	switch value {
	case "decision":
		return untilDecision, nil
	case "terminal":
		return untilTerminal, nil
	default:
		return 0, invocation.UsageFailure("--until must be decision or terminal")
	}
}

func (u waitUntil) holds(state action_servicepb.ActionState) bool {
	if u == untilDecision {
		return actionstate.Decided(state)
	}
	return actionstate.Terminal(state)
}

// exitStatus is what a state that holds exits with. It never derives success from the result.
func exitStatus(state action_servicepb.ActionState) (int, error) {
	switch state {
	case action_servicepb.ActionState_ACTION_STATE_SUCCEEDED,
		action_servicepb.ActionState_ACTION_STATE_ALLOWED,
		action_servicepb.ActionState_ACTION_STATE_DISPATCHING,
		action_servicepb.ActionState_ACTION_STATE_RUNNING:
		return 0, nil
	case action_servicepb.ActionState_ACTION_STATE_DENIED:
		return invocation.ExitDenied, nil
	case action_servicepb.ActionState_ACTION_STATE_FAILED:
		return invocation.ExitFailed, nil
	case action_servicepb.ActionState_ACTION_STATE_CANCELLED:
		return invocation.ExitCancelled, nil
	case action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		return invocation.ExitExecutionUnknown, nil
	default:
		return 0, invocation.InvalidResponse("unrecognized Action state %q", state)
	}
}

func readReceipt(ctx context.Context, client action_servicepb.ActionServiceClient, id string) (*action_servicepb.ActionRequest, error) {
	response, err := client.GetActionRequest(ctx, &action_servicepb.GetActionRequestRequest{RequestId: id})
	if err != nil {
		return nil, invocation.GRPCFailure(ctx, err)
	}
	return checkedReceipt(response.GetActionRequest())
}

// lookupByKey reads the request submitted under an idempotency key.
func lookupByKey(ctx context.Context, client action_servicepb.ActionServiceClient, key string) (*action_servicepb.ActionRequest, error) {
	response, err := client.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{IdempotencyKey: &key})
	if err != nil {
		return nil, invocation.GRPCFailure(ctx, err)
	}
	switch len(response.GetActionRequests()) {
	case 0:
		return nil, invocation.ClientFailure(invocation.ExitNotFound, "NOT_FOUND", "no Action request was submitted under idempotency key %q", key)
	case 1:
		return checkedReceipt(response.GetActionRequests()[0])
	default:
		return nil, invocation.InvalidResponse("%d requests under one idempotency key", len(response.GetActionRequests()))
	}
}

// await retries the initial read only while the command has a deadline.
func (t target) await(ctx context.Context, client action_servicepb.ActionServiceClient, until waitUntil) (*action_servicepb.ActionRequest, error) {
	_, bounded := ctx.Deadline()
	backoff := newBackoff()
	for {
		last, err := t.read(ctx, client)
		if err == nil {
			return watchReceipt(ctx, client, until, last)
		}
		var failed *invocation.Failure
		if !errors.As(err, &failed) || failed.Exit != invocation.ExitTransient || !bounded || ctx.Err() != nil {
			return nil, err
		}
		pause(ctx, backoff, failed)
	}
}

// A watch starts with the current receipt, so a change since the read or submission cannot be lost.
func watchReceipt(ctx context.Context, client action_servicepb.ActionServiceClient, until waitUntil, last *action_servicepb.ActionRequest) (*action_servicepb.ActionRequest, error) {
	_, bounded := ctx.Deadline()
	if until.holds(last.GetState()) || !bounded {
		return last, nil
	}
	backoff := newBackoff()
	var lastFailure error
	for {
		if ctx.Err() == context.Canceled {
			return last, invocation.ErrInterrupted
		}
		if invocation.DeadlineElapsed(ctx) {
			return last, lastFailure
		}
		stream, err := client.WatchActionRequest(ctx, &action_servicepb.WatchActionRequestRequest{
			RequestId: last.GetId(), Until: action_servicepb.WatchUntil(until),
		})
		sawFrame := false
		for err == nil {
			var frame *action_servicepb.WatchActionRequestResponse
			frame, err = stream.Recv()
			if errors.Is(err, io.EOF) {
				return last, invocation.InvalidResponse("WatchActionRequest ended before the condition held")
			}
			if err != nil {
				break
			}
			sawFrame = true
			last, err = checkedReceipt(frame.GetActionRequest())
			if err != nil {
				return nil, err
			}
			lastFailure, backoff = nil, newBackoff()
			if until.holds(last.GetState()) {
				return last, nil
			}
		}
		if ctx.Err() == context.Canceled {
			return last, invocation.ErrInterrupted
		}
		if invocation.DeadlineElapsed(ctx) {
			return last, lastFailure
		}
		if sawFrame && status.Code(err) == codes.Unauthenticated {
			// A projected token can expire during a watch. Reopening lets the egress sidecar
			// supply its current token. A refusal before the next frame is final.
			continue
		}
		rpcErr := invocation.GRPCFailure(ctx, err)
		var failed *invocation.Failure
		if !errors.As(rpcErr, &failed) || failed.Exit != invocation.ExitTransient {
			return last, rpcErr
		}
		lastFailure = rpcErr
		pause(ctx, backoff, failed)
	}
}

func newBackoff() retry.Backoff {
	return retry.WithCappedDuration(4*time.Second, retry.NewExponential(250*time.Millisecond))
}

// pause sleeps before the next attempt, for as long as the service asked if it did.
func pause(ctx context.Context, backoff retry.Backoff, after *invocation.Failure) {
	delay, _ := backoff.Next()
	if after.RetryAfterSeconds != nil {
		delay = max(delay, time.Duration(*after.RetryAfterSeconds)*time.Second)
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

// finish prints the receipt a wait ended with and exits as its state says: 10 when the condition
// had not held by the deadline.
func (a *commands) finish(last *action_servicepb.ActionRequest, until waitUntil) error {
	if last == nil {
		return invocation.InvalidResponse("wait ended without a receipt")
	}
	code := invocation.ExitDeadline
	if until.holds(last.GetState()) {
		var err error
		if code, err = exitStatus(last.GetState()); err != nil {
			return err
		}
	}
	if err := a.printReceipt(last); err != nil {
		return err
	}
	return invocation.ExitWith(code)
}

// afterWait is the end of a wait that returned an error. A wait that lost the service after it had
// read a receipt prints that receipt, the one error exit that carries stdout.
func (a *commands) afterWait(last *action_servicepb.ActionRequest, err error) error {
	var failed *invocation.Failure
	if last != nil && errors.As(err, &failed) && failed.Exit == invocation.ExitTransient {
		if printErr := a.printReceipt(last); printErr != nil {
			return printErr
		}
	}
	return err
}
