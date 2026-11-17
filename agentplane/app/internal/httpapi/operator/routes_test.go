package operator

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type operatorAPILinkageServer struct {
	linkage_servicepb.UnimplementedLinkageServiceServer
	complete func(context.Context, *linkage_servicepb.CompleteCallbackRequest) (*linkage_servicepb.CompleteCallbackResponse, error)
}

func (s *operatorAPILinkageServer) CompleteCallback(
	ctx context.Context,
	request *linkage_servicepb.CompleteCallbackRequest,
) (*linkage_servicepb.CompleteCallbackResponse, error) {
	return s.complete(ctx, request)
}

func newOperatorAPILinkageClient(t *testing.T, service linkage_servicepb.LinkageServiceServer) linkage_servicepb.LinkageServiceClient {
	t.Helper()
	server := grpc.NewServer()
	linkage_servicepb.RegisterLinkageServiceServer(server, service)
	address := grpcserver.Serve(t, server)
	connection, err := grpc.NewClient(address,
		grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return linkage_servicepb.NewLinkageServiceClient(connection)
}

func newOperatorHandler(callback linkage_servicepb.LinkageServiceClient) http.Handler {
	mux := http.NewServeMux()
	RegisterRoutes(mux, callback)
	return mux
}

func TestPublicMCPCallbackUsesLinkageGRPCAndRedirects(t *testing.T) {
	type callbackCall struct {
		request     *linkage_servicepb.CompleteCallbackRequest
		hasDeadline bool
		hasAuth     bool
	}
	calls := make(chan callbackCall, 1)
	callback := newOperatorAPILinkageClient(t, &operatorAPILinkageServer{complete: func(
		ctx context.Context,
		request *linkage_servicepb.CompleteCallbackRequest,
	) (*linkage_servicepb.CompleteCallbackResponse, error) {
		_, hasDeadline := ctx.Deadline()
		incomingMetadata, _ := metadata.FromIncomingContext(ctx)
		hasAuth := len(incomingMetadata.Get("authorization")) > 0
		calls <- callbackCall{request: request, hasDeadline: hasDeadline, hasAuth: hasAuth}
		return &linkage_servicepb.CompleteCallbackResponse{}, nil
	}})
	handler := newOperatorHandler(callback)
	values := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+values.Encode(), nil))
	if recorder.Code != http.StatusSeeOther || recorder.Header().Get("Location") != "/#/mcp-servers?linked=1" {
		t.Fatalf("callback response = %d, Location %q, body %s", recorder.Code, recorder.Header().Get("Location"), recorder.Body.String())
	}
	select {
	case call := <-calls:
		if call.request.GetState() != values.Get("state") || call.request.GetCode() != values.Get("code") || call.hasAuth || call.hasDeadline {
			t.Fatalf("LinkageService request = %#v, auth metadata=%v, deadline=%v", call.request, call.hasAuth, call.hasDeadline)
		}
	default:
		t.Fatal("LinkageService.CompleteCallback was not called")
	}
}

func TestPublicMCPCallbackRequiresStateAndCode(t *testing.T) {
	for _, query := range []string{"", "state=one", "code=two"} {
		handler := newOperatorHandler(nil)
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+query, nil))
		if recorder.Code != http.StatusBadRequest || recorder.Header().Get("Content-Type") != "text/plain; charset=utf-8" {
			t.Fatalf("missing callback parameters response = %d, headers %v", recorder.Code, recorder.Header())
		}
	}
}

func TestPublicMCPCallbackReportsMissingLinkageService(t *testing.T) {
	recorder := httptest.NewRecorder()
	newOperatorHandler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
		"/mcp-linkage/callback?state=one&code=two", nil))
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("unconfigured callback = %d", recorder.Code)
	}
}

func TestPublicMCPCallbackMapsLinkageGRPCFailuresWithoutLeakingDetails(t *testing.T) {
	tokenErr, err := status.New(codes.Unavailable, "provider body and token must remain private").WithDetails(&errdetails.ErrorInfo{
		Domain: "agentplane.actions", Reason: "MCP_TOKEN_EXCHANGE_FAILED",
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name       string
		err        error
		wantStatus int
	}{
		{name: "invalid flow", err: status.Error(codes.FailedPrecondition, "state capability invalid"), wantStatus: http.StatusConflict},
		{name: "unknown server", err: status.Error(codes.NotFound, "server id private"), wantStatus: http.StatusNotFound},
		{name: "empty value", err: status.Error(codes.InvalidArgument, "callback values missing"), wantStatus: http.StatusBadRequest},
		{name: "token exchange", err: tokenErr.Err(), wantStatus: http.StatusBadGateway},
		{name: "service unavailable", err: status.Error(codes.Unavailable, "the Action Service is temporarily unavailable"), wantStatus: http.StatusServiceUnavailable},
		{name: "transport unavailable", err: status.Error(codes.Unavailable, "dial error with private address"), wantStatus: http.StatusServiceUnavailable},
		{name: "other server failure", err: status.Error(codes.Internal, "private database error"), wantStatus: http.StatusServiceUnavailable},
		{name: "transport deadline", err: status.Error(codes.DeadlineExceeded, "private transport diagnostic"), wantStatus: http.StatusServiceUnavailable},
		{name: "backend canceled", err: status.Error(codes.Canceled, "private cancellation diagnostic"), wantStatus: http.StatusServiceUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			callback := newOperatorAPILinkageClient(t, &operatorAPILinkageServer{complete: func(
				context.Context,
				*linkage_servicepb.CompleteCallbackRequest,
			) (*linkage_servicepb.CompleteCallbackResponse, error) {
				calls++
				return nil, test.err
			}})
			handler := newOperatorHandler(callback)
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
				"/mcp-linkage/callback?state=capability-secret&code=authorization-secret", nil))
			if recorder.Code != test.wantStatus || recorder.Header().Get("Content-Type") != "text/plain; charset=utf-8" || calls != 1 {
				t.Fatalf("callback response = %d, headers %v, calls=%d", recorder.Code, recorder.Header(), calls)
			}
			for _, secret := range []string{"capability-secret", "authorization-secret", "provider body", "private address", "private database", "private transport diagnostic", "private cancellation diagnostic", "/v1/mcp-linkage/callback"} {
				if strings.Contains(recorder.Body.String(), secret) {
					t.Fatalf("callback response leaked %q: %s", secret, recorder.Body.String())
				}
			}
		})
	}
}

func TestPublicMCPCallbackCancellationReachesLinkageService(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	callback := newOperatorAPILinkageClient(t, &operatorAPILinkageServer{complete: func(
		ctx context.Context,
		_ *linkage_servicepb.CompleteCallbackRequest,
	) (*linkage_servicepb.CompleteCallbackResponse, error) {
		close(started)
		<-ctx.Done()
		close(canceled)
		return nil, status.Error(codes.Canceled, "request canceled")
	}})
	handler := newOperatorHandler(callback)
	requestContext, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?state=one&code=two", nil).WithContext(requestContext)
	recorder := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(recorder, request)
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		cancel()
		t.Fatal("LinkageService did not receive callback")
	}
	cancel()
	select {
	case <-canceled:
	case <-time.After(5 * time.Second):
		t.Fatal("HTTP request cancellation did not cancel the LinkageService call")
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("HTTP callback handler did not return after cancellation")
	}
	if recorder.Body.Len() != 0 {
		t.Fatalf("canceled callback wrote a response body: %q", recorder.Body.String())
	}
}
