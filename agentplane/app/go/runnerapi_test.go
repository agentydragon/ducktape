package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type runnerStatusFake struct {
	status *sessionpb.EnvironmentRuntimeStatus
	err    error
}

func (f runnerStatusFake) EnvironmentRuntimeStatus(context.Context, string, string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	return f.status, f.err
}

type runnerStatusTokenReviewer struct{}

func (runnerStatusTokenReviewer) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: []string{"agentplane"},
		User: authenticationv1.UserInfo{Username: "system:serviceaccount:fixture:reader"},
	}}, nil
}

// Exercise the generated AppService facade with lifecycle fields and integers beyond JS precision.
func TestEnvironmentRuntimeStatusServesLifecycleProgressAndExactCursors(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: runnerStatusTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:fixture:reader"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	if err := RegisterRunnerRoutes(server, &appService{runnerStatus: runnerStatusFake{status: &sessionpb.EnvironmentRuntimeStatus{
		EnvironmentRuntimeBootId: "fixture-boot",
		Lifecycle: &sessionpb.EnvironmentRuntimeLifecycleStatus{
			DesiredMode:       sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING,
			DesiredGeneration: 1<<63 + 1, ProgressGeneration: 7, ProgressAvailable: true,
			Stage: sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING,
			Blocker: &sessionpb.EnvironmentRuntimeLifecycleBlocker{
				Kind:         sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT,
				ResourceType: "IngressAcknowledgements.agentplane", ResourceId: "fixture-session", ResourcePresent: true,
				JournalSealPresent: true, JournalSealUid: "fixture-incarnation", ExpectedJournalSealUid: "fixture-incarnation",
				JournalSealCursor: 1<<63 + 3, AcknowledgementPresent: true, AcknowledgementCursor: 1<<63 + 2,
			},
		},
	}}}); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/sandboxes/fixture/runner-status?sandbox_uid=uid", nil)
	request.Header.Set("Authorization", "Bearer fixture-token")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("runner status = %d: %s", response.Code, response.Body.String())
	}
	var wire map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &wire); err != nil {
		t.Fatal(err)
	}
	if wire["environmentRuntimeBootId"] != "fixture-boot" {
		t.Fatalf("wire status = %s", response.Body.String())
	}
	if _, ok := wire["environment_runtime_boot_id"]; ok {
		t.Fatalf("ProtoJSON used proto field names: %s", response.Body.String())
	}
	processes, ok := wire["processes"].([]any)
	if !ok || len(processes) != 0 {
		t.Fatalf("empty repeated process field = %v: %s", wire["processes"], response.Body.String())
	}
	sessions, ok := wire["sessions"].([]any)
	if !ok || len(sessions) != 0 {
		t.Fatalf("empty repeated session field = %v: %s", wire["sessions"], response.Body.String())
	}
	lifecycleWire, ok := wire["lifecycle"].(map[string]any)
	if !ok || lifecycleWire["progressCurrent"] != false {
		t.Fatalf("default lifecycle fields = %v: %s", wire["lifecycle"], response.Body.String())
	}
	var body sessionpb.EnvironmentRuntimeStatus
	if err := protojson.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.GetEnvironmentRuntimeBootId() != "fixture-boot" {
		t.Fatalf("boot ID = %q", body.GetEnvironmentRuntimeBootId())
	}
	if len(body.GetProcesses()) != 0 || len(body.GetSessions()) != 0 {
		t.Fatalf("empty repeated fields = %v, %v", body.GetProcesses(), body.GetSessions())
	}
	lifecycle := body.GetLifecycle()
	if lifecycle.GetDesiredMode() != sessionpb.EnvironmentRuntimeLifecycleMode_ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING ||
		lifecycle.GetStage() != sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING ||
		lifecycle.GetDesiredGeneration() != 1<<63+1 || lifecycle.GetProgressGeneration() != 7 || lifecycle.GetProgressCurrent() {
		t.Fatalf("lifecycle progress = %+v", lifecycle)
	}
	if lifecycle.GetBlocker().GetKind() != sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT ||
		lifecycle.GetBlocker().GetResourceType() != "IngressAcknowledgements.agentplane" ||
		lifecycle.GetBlocker().GetJournalSealCursor() != 1<<63+3 || lifecycle.GetBlocker().GetAcknowledgementCursor() != 1<<63+2 {
		t.Fatalf("lifecycle blocker = %+v", lifecycle.GetBlocker())
	}
}

func TestRunnerStatusRoutePreservesSandboxErrorsAndCallerAuthentication(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: runnerStatusTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:fixture:reader"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	if err := RegisterRunnerRoutes(server, &appService{runnerStatus: runnerStatusFake{err: status.Error(codes.NotFound, "fixture")}}); err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodGet, "/sandboxes/fixture/runner-status?sandbox_uid=uid", nil)
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated runner status = %d: %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "/sandboxes/fixture/runner-status?sandbox_uid=uid", nil)
	request.Header.Set("Authorization", "Bearer fixture-token")
	response = httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("missing Sandbox runner status = %d: %s", response.Code, response.Body.String())
	}
	var body struct {
		Detail string `json:"detail"`
		Code   int32  `json:"code"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Detail != "fixture" || body.Code == 0 {
		t.Fatalf("missing Sandbox error = %+v", body)
	}
}
