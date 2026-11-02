package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"github.com/danielgtaylor/huma/v2"
)

func TestNewSandboxAcceptsPythonDefaultFieldsOmitted(t *testing.T) {
	server := NewHTTPServer(nil)
	called := false
	huma.Register[newSandboxInput, struct{}](server.API(), huma.Operation{
		OperationID: "createSandboxInputTest", Method: http.MethodPost, Path: "/sandbox-input-test",
	}, func(context.Context, *newSandboxInput) (*struct{}, error) {
		called = true
		return &struct{}{}, nil
	})

	request := httptest.NewRequest(http.MethodPost, "/sandbox-input-test", strings.NewReader(
		`{"name":"systest-model-1","template":"runner","preset":null,"policy_sets":[],"inbox_tags":[],"bootstrap":""}`,
	))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("POST /sandbox-input-test status = %d; body: %s", recorder.Code, recorder.Body.String())
	}
	if !called {
		t.Fatal("Huma rejected the Python-shaped Sandbox request before reaching the handler")
	}
}

func TestSandboxCreateAcknowledgementRetainsExactTarget(t *testing.T) {
	output := sandboxCreatedOutput(&environmentpb.TargetHandle{Namespace: "sandboxes", Name: "worker", Uid: "sandbox-uid"})
	if output.Body.Namespace != "sandboxes" || output.Body.Name != "worker" || output.Body.UID != "sandbox-uid" {
		t.Fatalf("create acknowledgement = %+v, want exact created namespace, name, and UID", output.Body)
	}
}
