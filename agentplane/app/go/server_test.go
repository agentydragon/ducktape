package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/danielgtaylor/huma/v2"
)

func TestHTTPServerHealthReadinessAndOpenAPI(t *testing.T) {
	server := NewHTTPServer(nil)
	handler := server.Handler(nil)

	for _, path := range []string{"/healthz", "/readyz"} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, path, nil))
		if recorder.Code != http.StatusNoContent {
			t.Fatalf("GET %s status = %d, want 204", path, recorder.Code)
		}
	}

	server.BeginDrain()
	select {
	case <-server.DrainDone():
	default:
		t.Fatal("server drain signal did not close when drain began")
	}
	server.BeginDrain() // Repeated signal handling must remain safe.
	if server.Ready() {
		t.Fatal("server reports ready after drain began")
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("GET /readyz while draining status = %d, want 503", recorder.Code)
	}

	recorder = httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d, want 200; body: %s", recorder.Code, recorder.Body.String())
	}
}

func TestProtectedAndOperatorGroupsUseTheAuthBoundary(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	called := false
	type emptyInput struct{}
	type output struct {
		Body struct {
			OK bool `json:"ok"`
		}
	}
	register := func(group huma.API, path, id string) {
		huma.Register[emptyInput, output](group, huma.Operation{OperationID: id, Method: http.MethodGet, Path: path}, func(context.Context, *emptyInput) (*output, error) {
			called = true
			return &output{}, nil
		})
	}
	register(server.ProtectedAPI(), "/private", "private")
	register(server.OperatorAPI(), "/operator", "operator")
	handler := server.Handler(authentication)

	for _, test := range []struct {
		path string
		want int
	}{
		{path: "/private", want: http.StatusUnauthorized},
		{path: "/operator", want: http.StatusUnauthorized},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, test.path, nil))
		if recorder.Code != test.want {
			t.Fatalf("GET %s status = %d, want %d; body: %s", test.path, recorder.Code, test.want, recorder.Body.String())
		}
	}
	if called {
		t.Fatal("protected operation handler ran without its required caller")
	}
}

func TestHumaResponsesDoNotAddSchemaMetadata(t *testing.T) {
	server := NewHTTPServer(nil)
	type emptyInput struct{}
	type output struct {
		Body struct {
			Value string `json:"value"`
		}
	}
	huma.Register[emptyInput, output](server.API(), huma.Operation{
		OperationID: "schemaMetadataTest", Method: http.MethodGet, Path: "/schema-metadata-test",
	}, func(context.Context, *emptyInput) (*output, error) {
		result := &output{}
		result.Body.Value = "present"
		return result, nil
	})

	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/schema-metadata-test", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("GET /schema-metadata-test status = %d, want 200; body: %s", recorder.Code, recorder.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response body: %v", err)
	}
	if body["value"] != "present" {
		t.Fatalf("response body = %#v, want value present", body)
	}
	if _, ok := body["$schema"]; ok {
		t.Fatalf("Huma schema metadata leaked into the response body: %#v", body)
	}
	if got := recorder.Header().Get("Link"); got != "" {
		t.Fatalf("Huma schema Link header = %q, want none", got)
	}
}
