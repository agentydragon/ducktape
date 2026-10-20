package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/sethvargo/go-retry"
)

const (
	// maxServerWait is the longest wait asked of the server in one request: the Action Service's
	// default max_wait_seconds. Longer budgets re-issue the wait.
	maxServerWait = 30 * time.Second
	// waitMargin keeps a server wait shorter than the deadline, so the server answers with the
	// current receipt before the client gives up on the request.
	waitMargin = 500 * time.Millisecond
	// minServerWait is the shortest wait worth a request.
	minServerWait = 50 * time.Millisecond
)

// receipt is an ActionRequest as the Action Service returned it. raw is what it sent.
type receipt struct {
	raw    json.RawMessage
	ID     string `json:"id"`
	State  string `json:"state"`
	Title  string `json:"title"`
	Action struct {
		Group string `json:"group"`
		Name  string `json:"name"`
	} `json:"action"`
	Decision *struct {
		Note   *string `json:"decision_note"`
		Reason *string `json:"reason_description"`
	} `json:"decision"`
	Execution *struct {
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	} `json:"execution"`
}

func parseReceipt(raw []byte) (*receipt, error) {
	parsed := &receipt{raw: raw}
	if err := json.Unmarshal(raw, parsed); err != nil {
		return nil, invalidResponse("%v", err)
	}
	return parsed, nil
}

// waitUntil is a condition on a receipt, in the Action Service's own terms.
type waitUntil string

const (
	untilDecision waitUntil = "decision"
	untilTerminal waitUntil = "terminal"
)

func parseUntil(value string) (waitUntil, error) {
	switch until := waitUntil(value); until {
	case untilDecision, untilTerminal:
		return until, nil
	}
	return "", usageFailure("--until must be decision or terminal")
}

// holds is the Action Service's rule: `decision` once the state is not decision_pending, `terminal`
// once it is none of decision_pending, allowed, dispatching or running. A state it has never heard
// of holds, so finish refuses it instead of the wait going on.
func (u waitUntil) holds(state string) bool {
	switch state {
	case "decision_pending":
		return false
	case "allowed", "dispatching", "running":
		return u == untilDecision
	}
	return true
}

// exitStatus is what a state that holds exits with. It is the platform's answer from the Action's
// state, never from its result.
func (r *receipt) exitStatus() (int, error) {
	switch r.State {
	case "succeeded", "allowed", "dispatching", "running":
		return 0, nil
	case "denied":
		return exitDenied, nil
	case "failed":
		return exitFailed, nil
	case "cancelled":
		return exitCancelled, nil
	case "execution_unknown":
		return exitExecutionUnknown, nil
	}
	return 0, invalidResponse("unrecognized Action state %q", r.State)
}

// reader reads the current receipt. A positive wait asks the server to hold the answer until the
// condition holds or that many seconds pass.
type reader func(ctx context.Context, wait float64) (*receipt, error)

func readReceipt(ctx context.Context, svc *service, id string, until waitUntil, wait float64) (*receipt, error) {
	query := url.Values{}
	if wait > 0 {
		query.Set("wait_seconds", strconv.FormatFloat(wait, 'f', 3, 64))
		query.Set("wait_until", string(until))
	}
	body, err := svc.do(ctx, request{method: http.MethodGet, path: "/v1/action-requests/" + url.PathEscape(id), query: query})
	if err != nil {
		return nil, err
	}
	return parseReceipt(body)
}

// lookupByKey reads the request submitted under an idempotency key.
func lookupByKey(ctx context.Context, svc *service, key string) (*receipt, error) {
	body, err := svc.do(ctx, request{method: http.MethodGet, path: "/v1/action-requests", query: url.Values{"idempotency_key": {key}}})
	if err != nil {
		return nil, err
	}
	items, err := decode[[]json.RawMessage](body)
	if err != nil {
		return nil, err
	}
	switch len(items) {
	case 0:
		return nil, clientFailure(exitNotFound, "NOT_FOUND", "no Action request was submitted under idempotency key %q", key)
	case 1:
		return parseReceipt(items[0])
	}
	return nil, invalidResponse("%d requests under one idempotency key", len(items))
}

// waitSlice is how long to ask the server to wait, within what is left of the deadline.
func waitSlice(ctx context.Context) time.Duration {
	deadline, bounded := ctx.Deadline()
	if !bounded {
		return 0
	}
	if slice := min(time.Until(deadline)-waitMargin, maxServerWait); slice >= minServerWait {
		return slice
	}
	return 0
}

// await reads the receipt until its state satisfies until or the deadline of ctx. It re-issues
// server-side waits, never polling; between failed attempts it backs off. last is the latest
// receipt read (it starts as given). The result:
//
//   - nil error and a receipt that holds: the condition was reached;
//   - nil error and one that does not: the deadline came first while the service was answering;
//   - an error: the service was unreachable when the deadline came, or the failure was not transient.
func await(ctx context.Context, until waitUntil, last *receipt, read reader) (*receipt, error) {
	_, bounded := ctx.Deadline()
	var lastFailure error
	backoff := newBackoff()
	for {
		if last != nil && until.holds(last.State) {
			return last, nil
		}
		var slice time.Duration
		if last != nil {
			if slice = waitSlice(ctx); slice == 0 {
				return last, lastFailure
			}
		}
		next, err := read(ctx, slice.Seconds())
		if err == nil {
			last, lastFailure, backoff = next, nil, newBackoff()
			continue
		}
		var failed *failure
		if !errors.As(err, &failed) || failed.exit != exitTransient || !bounded {
			return last, err
		}
		if ctx.Err() != nil {
			// The deadline ended this request. After an outage, that outage is the failure; with a
			// healthy service before it, the deadline was reached.
			switch {
			case lastFailure != nil:
				return last, lastFailure
			case last != nil:
				return last, nil
			}
			return last, err
		}
		lastFailure = err
		pause(ctx, backoff, failed)
	}
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
	code := exitDeadline
	if until.holds(last.State) {
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
