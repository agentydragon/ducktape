package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

type operatorAPICallback struct {
	response *http.Response
	err      error
	method   string
	path     string
	query    string
	bearer   string
	headers  http.Header
}

func (c *operatorAPICallback) OpenRequest(
	_ context.Context,
	method, path, query, bearer string,
	headers http.Header,
	_ io.Reader,
) (*http.Response, error) {
	c.method, c.path, c.query, c.bearer, c.headers = method, path, query, bearer, headers.Clone()
	return c.response, c.err
}

type operatorAPICloseTracker struct{ closed bool }

func (c *operatorAPICloseTracker) Read([]byte) (int, error) { return 0, io.EOF }
func (c *operatorAPICloseTracker) Close() error             { c.closed = true; return nil }

func TestPublicMCPCallbackForwardsQueryAndRedirects(t *testing.T) {
	body := &operatorAPICloseTracker{}
	callback := &operatorAPICallback{response: &http.Response{StatusCode: http.StatusOK, Body: body}}
	server := NewHTTPServer(nil)
	RegisterOperatorRoutes(server, callback, "https://actions.example.test")
	values := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	request := httptest.NewRequest(http.MethodGet, "/mcp-linkage/callback?"+values.Encode(), nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusSeeOther || recorder.Header().Get("Location") != "/#/mcp-servers?linked=1" {
		t.Fatalf("callback response = %d, Location %q, body %s", recorder.Code, recorder.Header().Get("Location"), recorder.Body.String())
	}
	if callback.method != http.MethodGet || callback.path != "v1/mcp-linkage/callback" ||
		callback.bearer != "" || callback.headers.Get("Accept") != "application/json" || !body.closed {
		t.Fatalf("callback upstream call = method %q path %q bearer %q headers %v closed %v",
			callback.method, callback.path, callback.bearer, callback.headers, body.closed)
	}
	want := url.Values{"state": {"state +&"}, "code": {"code/with?chars"}}
	if callback.query != want.Encode() {
		t.Fatalf("callback query = %q, want %q", callback.query, want.Encode())
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
		RegisterOperatorRoutes(server, nil, "")
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

func TestPublicMCPCallbackPreservesPythonFailureStatuses(t *testing.T) {
	for _, test := range []struct {
		name       string
		status     int
		wantStatus int
		wantDetail string
	}{
		{name: "redirect", status: http.StatusFound, wantStatus: http.StatusBadGateway, wantDetail: "Action callback redirects are not accepted"},
		{name: "callback refusal", status: http.StatusConflict, wantStatus: http.StatusConflict, wantDetail: "MCP linkage callback failed"},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := &operatorAPICloseTracker{}
			callback := &operatorAPICallback{response: &http.Response{StatusCode: test.status, Body: body}}
			server := NewHTTPServer(nil)
			RegisterOperatorRoutes(server, callback, "https://actions.example.test")
			recorder := httptest.NewRecorder()
			server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
				"/mcp-linkage/callback?state=one&code=two", nil))
			var response map[string]string
			if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if recorder.Code != test.wantStatus || response["detail"] != test.wantDetail || !body.closed {
				t.Fatalf("callback response = %d %v closed=%v", recorder.Code, response, body.closed)
			}
		})
	}
}

func TestPublicMCPCallbackReportsConfigurationAndTransportFailureSafely(t *testing.T) {
	t.Run("unconfigured", func(t *testing.T) {
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, nil, "https://actions.example.test")
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
			"/mcp-linkage/callback?state=one&code=two", nil))
		var response map[string]string
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusServiceUnavailable || response["detail"] != "Action facade is not configured" {
			t.Fatalf("unconfigured callback = %d %v", recorder.Code, response)
		}
	})

	t.Run("transport error is credential free", func(t *testing.T) {
		callback := &operatorAPICallback{err: errors.New("dial failed")}
		server := NewHTTPServer(nil)
		RegisterOperatorRoutes(server, callback, "https://user:secret@actions.example.test/base?token=hidden")
		recorder := httptest.NewRecorder()
		server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet,
			"/mcp-linkage/callback?state=one&code=two", nil))
		var response struct {
			Detail struct {
				Method         string `json:"method"`
				URL            string `json:"url"`
				UpstreamStatus *int   `json:"upstream_status"`
				ErrorType      string `json:"error_type"`
			} `json:"detail"`
		}
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if recorder.Code != http.StatusServiceUnavailable || response.Detail.Method != http.MethodGet ||
			response.Detail.URL != "https://actions.example.test/v1/mcp-linkage/callback" ||
			response.Detail.UpstreamStatus != nil || response.Detail.ErrorType != "RequestError" {
			t.Fatalf("transport callback detail = %d %#v", recorder.Code, response.Detail)
		}
	})
}
