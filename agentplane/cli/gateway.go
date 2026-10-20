package main

import (
	"encoding/json"
	"math"

	"google.golang.org/genproto/googleapis/rpc/code"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/encoding/protojson"
)

const retryInfoType = "type.googleapis.com/google.rpc.RetryInfo"

// gatewayError is a `google.rpc.Status`, the body a gRPC-Gateway facade answers an error with. It names
// the gRPC code, which its HTTP status only approximates: `FAILED_PRECONDITION` and `OUT_OF_RANGE` both
// arrive as 400.
type gatewayError struct {
	code string
	exit int
	// retryAfter is the `google.rpc.RetryInfo` delay in whole seconds, if the status carries one.
	retryAfter *int
}

// parseGatewayError reads a `google.rpc.Status` body. A body without a nonzero numeric `code` is not one.
func parseGatewayError(body []byte) (gatewayError, bool) {
	var status struct {
		Code    int32             `json:"code"`
		Details []json.RawMessage `json:"details"`
	}
	if json.Unmarshal(body, &status) != nil || status.Code == 0 {
		return gatewayError{}, false
	}
	known, ok := code.Code_name[status.Code]
	if !ok {
		return gatewayError{}, false
	}
	parsed := gatewayError{code: known, exit: exitForCode(code.Code(status.Code))}
	for _, detail := range status.Details {
		var typed struct {
			Type string `json:"@type"`
		}
		if json.Unmarshal(detail, &typed) != nil || typed.Type != retryInfoType {
			continue
		}
		var info errdetails.RetryInfo
		if (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(detail, &info) == nil && info.RetryDelay != nil {
			seconds := max(0, int(math.Ceil(info.RetryDelay.AsDuration().Seconds())))
			parsed.retryAfter = &seconds
		}
	}
	return parsed, true
}

// exitForCode is the exit status of a gRPC code, by the same classes httpFailure gives HTTP statuses.
func exitForCode(c code.Code) int {
	switch c {
	case code.Code_NOT_FOUND:
		return exitNotFound
	case code.Code_ALREADY_EXISTS, code.Code_ABORTED:
		return exitConflict
	case code.Code_RESOURCE_EXHAUSTED, code.Code_UNAVAILABLE, code.Code_DEADLINE_EXCEEDED:
		return exitTransient
	default:
		return exitRefused
	}
}
