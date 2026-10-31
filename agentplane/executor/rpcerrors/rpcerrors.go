package rpcerrors

import (
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/status"
)

const (
	// GatewayDomain identifies structured errors emitted by Agentplane's Gateway.
	GatewayDomain = "agentplane.allegedly.works"

	// OpenSessionNotDispatchedReason means Gateway admission failed before calling Runner.OpenSession.
	OpenSessionNotDispatchedReason = "OPEN_SESSION_NOT_DISPATCHED"
)

// WithOpenSessionNotDispatched adds machine-readable proof that Gateway rejected OpenSession before
// forwarding it to Runner. Unexpected local errors are preserved without an annotation.
func WithOpenSessionNotDispatched(err error) error {
	if err == nil {
		return nil
	}
	rpcStatus, ok := status.FromError(err)
	if !ok {
		return err
	}
	annotated, detailErr := rpcStatus.WithDetails(&errdetails.ErrorInfo{
		Domain: GatewayDomain,
		Reason: OpenSessionNotDispatchedReason,
		Metadata: map[string]string{
			"dispatch_state": "not_dispatched",
			"method":         "Executor.OpenSession",
			"stage":          "execution_connection",
		},
	})
	if detailErr != nil {
		return err
	}
	return annotated.Err()
}

// IsOpenSessionNotDispatched matches only Gateway's explicit OpenSession admission evidence.
func IsOpenSessionNotDispatched(err error) bool {
	if err == nil {
		return false
	}
	rpcStatus, ok := status.FromError(err)
	if !ok {
		return false
	}
	for _, detail := range rpcStatus.Details() {
		info, ok := detail.(*errdetails.ErrorInfo)
		if ok && isOpenSessionNotDispatched(info) {
			return true
		}
	}
	return false
}

// WithoutOpenSessionNotDispatched removes the reserved marker from errors returned after Gateway
// has forwarded OpenSession. It preserves the original status and all unrelated details.
func WithoutOpenSessionNotDispatched(err error) error {
	if err == nil {
		return nil
	}
	rpcStatus, ok := status.FromError(err)
	if !ok {
		return err
	}
	wire := rpcStatus.Proto()
	kept := wire.Details[:0]
	stripped := false
	for _, detail := range wire.Details {
		if detail == nil {
			kept = append(kept, detail)
			continue
		}
		info := &errdetails.ErrorInfo{}
		if detail.UnmarshalTo(info) == nil && isOpenSessionNotDispatched(info) {
			stripped = true
			continue
		}
		kept = append(kept, detail)
	}
	if !stripped {
		return err
	}
	wire.Details = kept
	return status.FromProto(wire).Err()
}

func isOpenSessionNotDispatched(info *errdetails.ErrorInfo) bool {
	return info.GetDomain() == GatewayDomain && info.GetReason() == OpenSessionNotDispatchedReason &&
		info.GetMetadata()["dispatch_state"] == "not_dispatched" &&
		info.GetMetadata()["method"] == "Executor.OpenSession" &&
		info.GetMetadata()["stage"] == "execution_connection"
}
