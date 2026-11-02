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
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func sandboxString(value string) *string { return &value }

type resumeSandboxGetter struct{ sandbox *sandboxespb.Sandbox }

func (g resumeSandboxGetter) Get(context.Context, string) (*sandboxespb.Sandbox, error) {
	return g.sandbox, nil
}

type resumeSandboxClient struct {
	request  *lifecyclepb.ResumeEnvironmentRequest
	response *lifecyclepb.ResumeEnvironmentResponse
	err      error
}

func (c *resumeSandboxClient) ResumeEnvironment(
	_ context.Context,
	request *lifecyclepb.ResumeEnvironmentRequest,
) (*lifecyclepb.ResumeEnvironmentResponse, error) {
	c.request = request
	return c.response, c.err
}

func TestOperatorResumeUsesCurrentExactSandboxIdentityThroughLifecycle(t *testing.T) {
	current := &sandboxespb.Sandbox{Namespace: "sandboxes", Name: "worker", Uid: "sandbox-uid"}
	lifecycle := &resumeSandboxClient{response: &lifecyclepb.ResumeEnvironmentResponse{
		Target:  &discoverypb.TargetHandle{Namespace: current.Namespace, Name: current.Name, Uid: current.Uid},
		Outcome: lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED,
	}}
	if err := requestSandboxResume(context.Background(), "sandboxes", "worker", resumeSandboxGetter{sandbox: current}, lifecycle); err != nil {
		t.Fatal(err)
	}
	if lifecycle.request.GetTarget().GetNamespace() != current.Namespace || lifecycle.request.GetTarget().GetName() != current.Name ||
		lifecycle.request.GetTarget().GetUid() != current.Uid || lifecycle.request.GetResumeToken() != "" {
		t.Fatalf("operator resume request = %v", lifecycle.request)
	}
}

func TestOperatorResumeReportsChangedStateForRefresh(t *testing.T) {
	current := &sandboxespb.Sandbox{Namespace: "sandboxes", Name: "worker", Uid: "sandbox-uid"}
	lifecycle := &resumeSandboxClient{response: &lifecyclepb.ResumeEnvironmentResponse{
		Target:  &discoverypb.TargetHandle{Namespace: current.Namespace, Name: current.Name, Uid: current.Uid},
		Outcome: lifecyclepb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED,
	}}
	err := requestSandboxResume(context.Background(), "sandboxes", "worker", resumeSandboxGetter{sandbox: current}, lifecycle)
	if status.Code(err) != codes.Aborted {
		t.Fatalf("state changed error = %v, want Aborted", err)
	}
}

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
	sessionID := "session-a"
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
		IdleSuspendAt: timestamppb.New(time.Date(2026, 10, 1, 2, 5, 4, 0, time.UTC)),
		SuspendedBy:   sandboxString("Idle"), Phase: sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING,
		AutosuspendBlockers: []*sandboxespb.AutosuspendBlocker{
			{Reason: "ActiveTurn", SessionId: &sessionID},
			{Reason: "Bootstrap"},
		},
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
	if object["idle_suspend_at"] != "2026-10-01T02:05:04Z" {
		t.Fatalf("browser idle deadline = %v", object["idle_suspend_at"])
	}
	blockers, ok := object["autosuspend_blockers"].([]any)
	if !ok || len(blockers) != 2 {
		t.Fatalf("browser autosuspend blockers = %v, want two rows", object["autosuspend_blockers"])
	}
	firstBlocker, ok := blockers[0].(map[string]any)
	if !ok || firstBlocker["reason"] != "ActiveTurn" || firstBlocker["session_id"] != sessionID {
		t.Fatalf("session-scoped browser blocker = %v", blockers[0])
	}
	secondBlocker, ok := blockers[1].(map[string]any)
	if !ok || secondBlocker["reason"] != "Bootstrap" || secondBlocker["session_id"] != nil {
		t.Fatalf("global browser blocker = %v", blockers[1])
	}
	if _, ok := object["createdAt"]; ok {
		t.Fatalf("browser JSON unexpectedly uses protobuf camelCase: %s", body)
	}
}

func TestSandboxViewSerializesEmptyAutosuspendBlockersAsEmptyArray(t *testing.T) {
	body, err := json.Marshal(sandboxView(&sandboxespb.Sandbox{}))
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	if blockers, ok := object["autosuspend_blockers"].([]any); !ok || len(blockers) != 0 {
		t.Fatalf("empty browser autosuspend blockers = %v, want []", object["autosuspend_blockers"])
	}
}

func TestSandboxViewProjectsEveryPhaseAndAbsentMode(t *testing.T) {
	phases := map[sandboxespb.SandboxPhase]string{
		sandboxespb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED:  "Unspecified",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_CREATING:     "Creating",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING:      "Running",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDING:   "Suspending",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED:    "Suspended",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_QUIESCING:    "Quiescing",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN: "Tearing down",
	}
	if len(phases) != len(sandboxespb.SandboxPhase_name) {
		t.Fatal("every service phase needs a browser projection")
	}
	for phase, want := range phases {
		t.Run(want, func(t *testing.T) {
			view := sandboxView(&sandboxespb.Sandbox{Phase: phase})
			if view.Phase != want || view.OperatingMode != nil {
				t.Fatalf("Sandbox projection = %+v", view)
			}
			body, err := json.Marshal(view)
			if err != nil {
				t.Fatal(err)
			}
			var object map[string]any
			if err := json.Unmarshal(body, &object); err != nil {
				t.Fatal(err)
			}
			if mode, ok := object["operating_mode"]; !ok || mode != nil {
				t.Fatalf("absent operating mode must be JSON null: %s", body)
			}
		})
	}
}
