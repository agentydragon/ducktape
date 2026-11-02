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
	var body struct {
		EnvironmentRuntimeBootId string `json:"environment_runtime_boot_id"`
		Processes                []any  `json:"processes"`
		Sessions                 []any  `json:"sessions"`
		Lifecycle                struct {
			DesiredMode        string `json:"desired_mode"`
			DesiredGeneration  string `json:"desired_generation"`
			ProgressGeneration string `json:"progress_generation"`
			ProgressCurrent    bool   `json:"progress_current"`
			Stage              string `json:"stage"`
			Blocker            struct {
				Kind                  string `json:"kind"`
				ResourceType          string `json:"resource_type"`
				JournalSealCursor     string `json:"journal_seal_cursor"`
				AcknowledgementCursor string `json:"acknowledgement_cursor"`
			} `json:"blocker"`
		} `json:"lifecycle"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.EnvironmentRuntimeBootId != "fixture-boot" {
		t.Fatalf("boot ID = %q", body.EnvironmentRuntimeBootId)
	}
	if body.Processes == nil || body.Sessions == nil {
		t.Fatalf("unpopulated repeated fields were omitted: %s", response.Body.String())
	}
	lifecycle := body.Lifecycle
	if lifecycle.DesiredMode != "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING" || lifecycle.Stage != "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING" ||
		lifecycle.DesiredGeneration != "9223372036854775809" || lifecycle.ProgressGeneration != "7" || lifecycle.ProgressCurrent {
		t.Fatalf("lifecycle progress = %+v", lifecycle)
	}
	if lifecycle.Blocker.Kind != "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT" ||
		lifecycle.Blocker.ResourceType != "IngressAcknowledgements.agentplane" ||
		lifecycle.Blocker.JournalSealCursor != "9223372036854775811" || lifecycle.Blocker.AcknowledgementCursor != "9223372036854775810" {
		t.Fatalf("lifecycle blocker = %+v", lifecycle.Blocker)
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
