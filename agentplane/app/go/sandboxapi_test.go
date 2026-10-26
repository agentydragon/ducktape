package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func sandboxString(value string) *string { return &value }

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

func TestSandboxViewKeepsBrowserFieldNamesAndDerivedValues(t *testing.T) {
	status, err := structpb.NewStruct(map[string]any{"conditions": []any{}, "ready": true})
	if err != nil {
		t.Fatal(err)
	}
	value := &sandboxespb.Sandbox{
		Name: "worker", Uid: "sandbox-uid", Namespace: "sandboxes", CreatedAt: timestamppb.New(time.Date(2026, 10, 1, 2, 3, 4, 0, time.UTC)),
		OperatingMode: sandboxString("Running"), Status: status,
		ServiceAccount: &sandboxespb.ServiceAccount{Namespace: "sandboxes", Name: "worker"},
		Binding:        &sandboxespb.SandboxBinding{Bootstrap: "echo ready"},
		Preset:         sandboxString("Default"), WakeOnSend: true, IdleSuspendAfter: durationpb.New(90 * time.Second),
		SuspendedBy: sandboxString("Idle"), Phase: sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING,
		Pod: &sandboxespb.SandboxPod{
			Name: "worker-pod", Namespace: "sandboxes", Uid: "pod-uid",
			OwnerReferences: []*sandboxespb.OwnerReference{{ApiVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: "worker", Uid: "sandbox-uid", Controller: true}},
		},
	}
	view := sandboxView(value)
	if view.Name != "worker" || view.Phase != "Running" || view.IdleSuspendAfterSeconds == nil || *view.IdleSuspendAfterSeconds != 90 {
		t.Fatalf("unexpected Sandbox view: %+v", view)
	}
	if view.Pod == nil || view.Pod.OwnerReferences[0].APIVersion != "agentplane.allegedly.works/v1alpha1" {
		t.Fatalf("unexpected Pod view: %+v", view.Pod)
	}
	body, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"created_at", "operating_mode", "service_account", "idle_suspend_after_seconds", "suspended_by"} {
		if _, ok := object[key]; !ok {
			t.Errorf("browser JSON lacks %q: %s", key, body)
		}
	}
	if _, ok := object["createdAt"]; ok {
		t.Fatalf("browser JSON unexpectedly uses protobuf camelCase: %s", body)
	}
}
