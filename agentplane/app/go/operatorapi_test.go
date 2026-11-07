package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
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
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	linkage_servicepb.RegisterLinkageServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	connection, err := grpc.NewClient(listener.Addr().String(),
		grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return linkage_servicepb.NewLinkageServiceClient(connection)
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
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, callback)
	values := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+values.Encode(), nil))
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
	for _, test := range []struct {
		query string
		want  []string
	}{
		{query: "", want: []string{"state", "code"}},
		{query: "state=one", want: []string{"code"}},
		{query: "code=two", want: []string{"state"}},
	} {
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, nil)
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+test.query, nil))
		var response struct {
			Detail []struct {
				Loc  []string `json:"loc"`
				Msg  string   `json:"msg"`
				Type string   `json:"type"`
			} `json:"detail"`
		}
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusUnprocessableEntity || len(response.Detail) != len(test.want) {
			t.Fatalf("missing callback parameters response = %d %#v", recorder.Code, response)
		}
		for i, name := range test.want {
			if len(response.Detail[i].Loc) != 2 || response.Detail[i].Loc[0] != "query" || response.Detail[i].Loc[1] != name ||
				response.Detail[i].Msg != "Field required" || response.Detail[i].Type != "missing" {
				t.Fatalf("missing callback parameter %q error = %#v", name, response.Detail[i])
			}
		}
	}
}

func TestPublicMCPCallbackReportsMissingLinkageService(t *testing.T) {
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
		"/mcp-linkage/callback?state=one&code=two", nil))
	var response map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusServiceUnavailable || response["detail"] != "Action LinkageService is not configured" {
		t.Fatalf("unconfigured callback = %d %v", recorder.Code, response)
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
		wantDetail string
	}{
		{name: "invalid flow", err: status.Error(codes.FailedPrecondition, "state capability invalid"), wantStatus: http.StatusConflict, wantDetail: "MCP linkage callback failed"},
		{name: "unknown server", err: status.Error(codes.NotFound, "server id private"), wantStatus: http.StatusNotFound, wantDetail: "MCP linkage callback failed"},
		{name: "empty value", err: status.Error(codes.InvalidArgument, "callback values missing"), wantStatus: http.StatusBadRequest, wantDetail: "MCP linkage callback failed"},
		{name: "token exchange", err: tokenErr.Err(), wantStatus: http.StatusBadGateway, wantDetail: "MCP linkage callback failed"},
		{name: "service unavailable", err: status.Error(codes.Unavailable, "the Action Service is temporarily unavailable"), wantStatus: http.StatusServiceUnavailable, wantDetail: "MCP linkage callback failed"},
		{name: "transport unavailable", err: status.Error(codes.Unavailable, "dial error with private address"), wantStatus: http.StatusServiceUnavailable, wantDetail: "MCP linkage callback failed"},
		{name: "other server failure", err: status.Error(codes.Internal, "private database error"), wantStatus: http.StatusServiceUnavailable, wantDetail: "MCP linkage callback failed"},
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
			server := NewHTTPServer(nil)
			RegisterOperatorRoutes(server, callback)
			recorder := httptest.NewRecorder()
			server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
				"/mcp-linkage/callback?state=capability-secret&code=authorization-secret", nil))
			var response map[string]string
			if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if recorder.Code != test.wantStatus || response["detail"] != test.wantDetail || calls != 1 {
				t.Fatalf("callback response = %d %v, calls=%d", recorder.Code, response, calls)
			}
			for _, secret := range []string{"capability-secret", "authorization-secret", "provider body", "private address", "private database"} {
				if strings.Contains(recorder.Body.String(), secret) {
					t.Fatalf("callback response leaked %q: %s", secret, recorder.Body.String())
				}
			}
		})
	}
}

func TestPublicMCPCallbackSanitizesTransportFailure(t *testing.T) {
	callback := newOperatorAPILinkageClient(t, &operatorAPILinkageServer{complete: func(
		context.Context,
		*linkage_servicepb.CompleteCallbackRequest,
	) (*linkage_servicepb.CompleteCallbackResponse, error) {
		return nil, status.Error(codes.DeadlineExceeded, "private transport diagnostic")
	}})
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, callback)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
		"/mcp-linkage/callback?state=capability-secret&code=authorization-secret", nil))
	var response struct {
		Detail struct {
			Service   string `json:"service"`
			Method    string `json:"method"`
			ErrorType string `json:"error_type"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusServiceUnavailable || response.Detail.Service != "agentplane.action_service.LinkageService" ||
		response.Detail.Method != "CompleteCallback" || response.Detail.ErrorType != codes.DeadlineExceeded.String() {
		t.Fatalf("transport callback detail = %d %#v", recorder.Code, response.Detail)
	}
	for _, secret := range []string{"/v1/mcp-linkage/callback", "capability-secret", "authorization-secret", "private transport diagnostic"} {
		if strings.Contains(recorder.Body.String(), secret) {
			t.Fatalf("callback response leaked %q: %s", secret, recorder.Body.String())
		}
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
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, callback)
	requestContext, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?state=one&code=two", nil).WithContext(requestContext)
	recorder := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		server.Handler(nil).ServeHTTP(recorder, request)
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
