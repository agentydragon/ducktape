package main

import (
	"context"
	"errors"
	"io"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

// receipt is the ActionRequest as the Action Service returned it.
type receipt struct {
	request *action_servicepb.ActionRequest
}

func receiptFromProto(message *action_servicepb.ActionRequest) (*receipt, error) {
	if err := validateActionRequest(message); err != nil {
		return nil, err
	}
	return &receipt{request: message}, nil
}

func validateActionRequest(message *action_servicepb.ActionRequest) error {
	if message == nil {
		return invalidResponse("response has no Action request")
	}
	switch message.GetState() {
	case action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING,
		action_servicepb.ActionState_ACTION_STATE_ALLOWED,
		action_servicepb.ActionState_ACTION_STATE_DENIED,
		action_servicepb.ActionState_ACTION_STATE_DISPATCHING,
		action_servicepb.ActionState_ACTION_STATE_RUNNING,
		action_servicepb.ActionState_ACTION_STATE_SUCCEEDED,
		action_servicepb.ActionState_ACTION_STATE_FAILED,
		action_servicepb.ActionState_ACTION_STATE_CANCELLED,
		action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
	default:
		return invalidResponse("unrecognized Action state %q", message.GetState())
	}
	return nil
}

func parseReceipt(raw []byte) (*action_servicepb.ActionRequest, error) {
	message := &action_servicepb.ActionRequest{}
	if err := protojson.Unmarshal(raw, message); err != nil {
		return nil, invalidResponse("%v", err)
	}
	if err := validateActionRequest(message); err != nil {
		return nil, err
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
		return 0, usageFailure("--until must be decision or terminal")
	}
}

// holds is the Action Service's rule: decision holds once the state is not decision_pending;
// terminal holds only at an execution outcome or a denial/cancellation.
func (u waitUntil) holds(state action_servicepb.ActionState) bool {
	switch state {
	case action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING:
		return false
	case action_servicepb.ActionState_ACTION_STATE_ALLOWED,
		action_servicepb.ActionState_ACTION_STATE_DISPATCHING,
		action_servicepb.ActionState_ACTION_STATE_RUNNING:
		return u == untilDecision
	case action_servicepb.ActionState_ACTION_STATE_DENIED,
		action_servicepb.ActionState_ACTION_STATE_SUCCEEDED,
		action_servicepb.ActionState_ACTION_STATE_FAILED,
		action_servicepb.ActionState_ACTION_STATE_CANCELLED,
		action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		return true
	default:
		return false
	}
}

// exitStatus is what a state that holds exits with. It never derives success from the result.
func (r *receipt) exitStatus() (int, error) {
	switch r.request.GetState() {
	case action_servicepb.ActionState_ACTION_STATE_SUCCEEDED,
		action_servicepb.ActionState_ACTION_STATE_ALLOWED,
		action_servicepb.ActionState_ACTION_STATE_DISPATCHING,
		action_servicepb.ActionState_ACTION_STATE_RUNNING:
		return 0, nil
	case action_servicepb.ActionState_ACTION_STATE_DENIED:
		return exitDenied, nil
	case action_servicepb.ActionState_ACTION_STATE_FAILED:
		return exitFailed, nil
	case action_servicepb.ActionState_ACTION_STATE_CANCELLED:
		return exitCancelled, nil
	case action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		return exitExecutionUnknown, nil
	default:
		return 0, invalidResponse("unrecognized Action state %q", r.request.GetState())
	}
}

type receiptReader func(context.Context) (*receipt, error)

func readReceipt(ctx context.Context, client action_servicepb.ActionServiceClient, id string) (*receipt, error) {
	response, err := client.GetActionRequest(ctx, &action_servicepb.GetActionRequestRequest{RequestId: id})
	if err != nil {
		return nil, grpcFailure(ctx, err)
	}
	return receiptFromProto(response.GetActionRequest())
}

// lookupByKey reads the request submitted under an idempotency key.
func lookupByKey(ctx context.Context, client action_servicepb.ActionServiceClient, key string) (*receipt, error) {
	response, err := client.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{IdempotencyKey: &key})
	if err != nil {
		return nil, grpcFailure(ctx, err)
	}
	switch len(response.GetActionRequests()) {
	case 0:
		return nil, clientFailure(exitNotFound, "NOT_FOUND", "no Action request was submitted under idempotency key %q", key)
	case 1:
		return receiptFromProto(response.GetActionRequests()[0])
	default:
		return nil, invalidResponse("%d requests under one idempotency key", len(response.GetActionRequests()))
	}
}

// await reads once for an unbounded invocation, otherwise follows WatchActionRequest until the
// condition holds or the command deadline arrives. A watch starts with the current receipt, so a
// change between the initial read and opening the stream cannot be lost.
func await(
	ctx context.Context,
	client action_servicepb.ActionServiceClient,
	until waitUntil,
	last *receipt,
	read receiptReader,
) (*receipt, error) {
	_, bounded := ctx.Deadline()
	backoff := newBackoff()
	var lastFailure error
	if last == nil {
		for {
			current, err := read(ctx)
			if err == nil {
				last, lastFailure, backoff = current, nil, newBackoff()
				break
			}
			var failed *failure
			if !errors.As(err, &failed) || failed.exit != exitTransient || !bounded {
				return nil, err
			}
			if ctx.Err() != nil {
				return nil, err
			}
			lastFailure = err
			pause(ctx, backoff, failed)
		}
	}
	if until.holds(last.request.GetState()) {
		return last, nil
	}
	if !bounded {
		return last, nil
	}
	for {
		if ctx.Err() == context.Canceled {
			return last, errInterrupted
		}
		if deadlineElapsed(ctx) {
			if lastFailure != nil {
				return last, lastFailure
			}
			return last, nil
		}
		stream, err := client.WatchActionRequest(ctx, &action_servicepb.WatchActionRequestRequest{
			RequestId: last.request.GetId(), Until: action_servicepb.WatchUntil(until),
		})
		if err != nil {
			if ctx.Err() == context.Canceled {
				return last, errInterrupted
			}
			if deadlineElapsed(ctx) {
				if lastFailure != nil {
					return last, lastFailure
				}
				return last, nil
			}
			rpcErr := grpcFailure(ctx, err)
			var failed *failure
			if !errors.As(rpcErr, &failed) || failed.exit != exitTransient {
				return last, rpcErr
			}
			lastFailure = rpcErr
			pause(ctx, backoff, failed)
			continue
		}
		sawFrame := false
		for {
			frame, err := stream.Recv()
			if err == nil {
				sawFrame = true
				last, err = receiptFromProto(frame.GetActionRequest())
				if err != nil {
					return nil, err
				}
				lastFailure, backoff = nil, newBackoff()
				if until.holds(last.request.GetState()) {
					return last, nil
				}
				continue
			}
			if errors.Is(err, io.EOF) {
				return last, invalidResponse("WatchActionRequest ended before the condition held")
			}
			if ctx.Err() == context.Canceled {
				return last, errInterrupted
			}
			if deadlineElapsed(ctx) {
				if lastFailure != nil {
					return last, lastFailure
				}
				return last, nil
			}
			if status.Code(err) == codes.Unauthenticated && sawFrame {
				// A long-lived watch can outlive the projected token. Opening a new RPC lets the
				// egress sidecar substitute its current token; refusal before a new frame is final.
				break
			}
			rpcErr := grpcFailure(ctx, err)
			var failed *failure
			if !errors.As(rpcErr, &failed) || failed.exit != exitTransient || !bounded {
				return last, rpcErr
			}
			lastFailure = rpcErr
			pause(ctx, backoff, failed)
			break
		}
	}
}

// deadlineElapsed recognizes the RPC deadline boundary even if the context timer has not yet
// published DeadlineExceeded to Err. grpc-go may surface that deadline just before ctx.Err flips.
func deadlineElapsed(ctx context.Context) bool {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return true
	}
	deadline, bounded := ctx.Deadline()
	return bounded && !time.Now().Before(deadline)
}

func newBackoff() retry.Backoff {
	return retry.WithCappedDuration(4*time.Second, retry.NewExponential(250*time.Millisecond))
}

// pause sleeps before the next attempt, for as long as the service asked if it did.
func pause(ctx context.Context, backoff retry.Backoff, after *failure) {
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
func (a *app) finish(last *receipt, until waitUntil) error {
	if last == nil {
		return invalidResponse("wait ended without a receipt")
	}
	code := exitDeadline
	if until.holds(last.request.GetState()) {
		var err error
		if code, err = last.exitStatus(); err != nil {
			return err
		}
	}
	if err := a.printReceipt(last); err != nil {
		return err
	}
	return exitWith(code)
}

// afterWait is the end of a wait that returned an error. A wait that lost the service after it had
// read a receipt prints that receipt, the one error exit that carries stdout.
func (a *app) afterWait(last *receipt, err error) error {
	var failed *failure
	if last != nil && errors.As(err, &failed) && failed.exit == exitTransient {
		if printErr := a.printReceipt(last); printErr != nil {
			return printErr
		}
	}
	return err
}
