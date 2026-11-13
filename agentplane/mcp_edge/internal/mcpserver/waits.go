package mcpserver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// errDisconnected ends a wait whose caller went away; there is no one to answer.
var errDisconnected = errors.New("the caller disconnected during the wait")

type transportContextKey struct{}

// withTransportContext hands tools the HTTP request's own context. A tool runs detached from cancellation (addTool),
// so that a call which changes state finishes after its caller left; a wait watches this context instead and ends
// with its caller. Ending a wait never cancels the Action.
func withTransportContext(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), transportContextKey{}, r.Context())))
	})
}

// read is the request's receipt now or, with a wait, once it satisfies wait.wait_until or the wait's deadline
// passes, whichever is first. A wait follows the core's receipt stream; nothing polls.
func (t *tools) read(ctx context.Context, requestID string, wait waitOptions) (*pb.ActionRequest, error) {
	if wait.WaitSeconds == 0 {
		response, err := t.core.GetActionRequest(ctx, &pb.GetActionRequestRequest{RequestId: requestID})
		if err != nil {
			return nil, requestError(err)
		}
		return response.GetActionRequest(), nil
	}
	var until pb.WatchUntil
	switch wait.WaitUntil {
	case "decision":
		until = pb.WatchUntil_WATCH_UNTIL_DECISION
	case "terminal":
		until = pb.WatchUntil_WATCH_UNTIL_TERMINAL
	default:
		return nil, fmt.Errorf("wait_until %q", wait.WaitUntil)
	}
	transport, ok := ctx.Value(transportContextKey{}).(context.Context)
	if !ok {
		return nil, errors.New("a wait outside an HTTP request")
	}
	watchCtx, cancel := context.WithTimeout(ctx, time.Duration(wait.WaitSeconds*float64(time.Second)))
	defer cancel()
	defer context.AfterFunc(transport, cancel)()
	stream, err := t.core.WatchActionRequest(watchCtx, &pb.WatchActionRequestRequest{RequestId: requestID, Until: until})
	var last *pb.ActionRequest
	for err == nil {
		var frame *pb.WatchActionRequestResponse
		if frame, err = stream.Recv(); err == nil {
			last = frame.GetActionRequest()
		}
	}
	// The deadline travels to the core as grpc-timeout, so the core can end the stream DEADLINE_EXCEEDED before this
	// side's clock reaches it.
	deadline := errors.Is(watchCtx.Err(), context.DeadlineExceeded) || status.Code(err) == codes.DeadlineExceeded
	switch {
	case errors.Is(err, io.EOF) && last != nil:
		return last, nil
	case transport.Err() != nil:
		return nil, errDisconnected
	case deadline && last != nil:
		return last, nil
	case deadline:
		// Nothing arrived before the deadline: the receipt as it stands.
		return t.read(ctx, requestID, waitOptions{})
	case errors.Is(err, io.EOF):
		return nil, errors.New("core ended a watch without a receipt")
	}
	return nil, watchError(err)
}
