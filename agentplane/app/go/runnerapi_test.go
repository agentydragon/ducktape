package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type runnerStatusFake struct{ status *sessionpb.EnvironmentRuntimeStatus }

func (f runnerStatusFake) EnvironmentRuntimeStatus(context.Context, string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	return f.status, nil
}

type runnerStatusTokenReviewer struct{}

func (runnerStatusTokenReviewer) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: []string{"agentplane"},
		User: authenticationv1.UserInfo{Username: "system:serviceaccount:fixture:reader"},
	}}, nil
}

// The raw-proto route is not represented by Huma's typed response models. Exercise the browser
// boundary when the protocol gains lifecycle fields, including integers beyond JS precision.
func TestEnvironmentRuntimeStatusServesLifecycleProgressAndExactCursors(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: runnerStatusTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:fixture:reader"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	RegisterRunnerRoutes(server, runnerStatusFake{status: &sessionpb.EnvironmentRuntimeStatus{
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
	}})
	request := httptest.NewRequest(http.MethodGet, "/sandboxes/fixture/runner-status", nil)
	request.Header.Set("Authorization", "Bearer fixture-token")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("runner status = %d: %s", response.Code, response.Body.String())
	}
	var body struct {
		Lifecycle struct {
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
