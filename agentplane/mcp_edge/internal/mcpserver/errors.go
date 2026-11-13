package mcpserver

import (
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// toolError is a refusal the caller reads as written. Every other failure is logged and masked.
type toolError string

func (e toolError) Error() string { return string(e) }

const (
	requestNotFound = toolError("Action request not found for this caller; use a request ID or idempotency key " +
		"this caller submitted.")
	keyAlreadyUsed = toolError("idempotency key already used by this caller; read that request by key instead " +
		"of resubmitting")
	groupUnavailable = toolError("ActionGroup is temporarily unavailable")
	updatesLost      = toolError("Action update channel unavailable; read the existing request with " +
		"wait_seconds=0. Do not submit a new idempotency key.")
	authorizationExpired = toolError("Caller authorization expired during the wait; reconnect with a valid caller " +
		"bearer.")
	unknownGroup      = toolError("Unknown group; omit group to discover configured Action identifiers.")
	afterElsewhere    = toolError("after.group must match the selected group; use the previous page's next_after.")
	nameOneRequest    = toolError("Name the request by exactly one of request_id or idempotency_key.")
	fieldsNeedReceipt = toolError(`include_fields shapes a receipt; pass respond_with="receipt" to get one.`)
	groupGone         = toolError("This Action's group is no longer configured; read the request with " +
		"get_action_request.")
)

func waitTooLong(maxWaitSeconds float64) toolError {
	return toolError(fmt.Sprintf("wait.wait_seconds must be at most %s seconds for this instance.", seconds(maxWaitSeconds)))
}

func unknownAction(group, name string) toolError {
	return toolError(fmt.Sprintf("unknown group/action (%q, %q)", group, name))
}

// maskedError answers a failure whose detail is not the caller's to read.
func maskedError(tool string) error {
	return fmt.Errorf("Error calling tool %q", tool)
}

// requestError maps a failed read or cancellation of one of the caller's requests.
func requestError(err error) error {
	if status.Code(err) == codes.NotFound {
		return requestNotFound
	}
	return err
}

// submitError maps a refused submission. The core's message is the refusal: which Action argument or group is
// wrong, or that the service is draining.
func submitError(err error) error {
	switch status.Code(err) {
	case codes.AlreadyExists:
		return keyAlreadyUsed
	case codes.InvalidArgument, codes.Unavailable:
		return toolError(status.Convert(err).Message())
	}
	return err
}

func actionError(err error, group, name string) error {
	switch status.Code(err) {
	case codes.NotFound:
		return unknownAction(group, name)
	case codes.Unavailable:
		return groupUnavailable
	}
	return err
}

// watchError maps a wait the core ended early.
func watchError(err error) error {
	switch status.Code(err) {
	case codes.NotFound:
		return requestNotFound
	case codes.Unavailable:
		return updatesLost
	case codes.Unauthenticated:
		return authorizationExpired
	}
	return err
}
