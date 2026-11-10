package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type systemStatusTokenReviewer struct{}

func (systemStatusTokenReviewer) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:fixture:reader"},
	}}, nil
}

func TestSystemStatusUsesGeneratedProtoJSONAndCallerAuthentication(t *testing.T) {
	checker, err := status.New([]status.Probe{
		{Name: "healthy", Check: func(context.Context) error { return nil }},
		{Name: "down", Check: func(context.Context) error { return errors.New("fixture unavailable") }},
	}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	authentication, err := auth.New(auth.Config{
		TokenReviews: systemStatusTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:fixture:reader"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	if err := RegisterStatusRoutes(server, &appService{systemStatus: checker}); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(authentication)

	request := httptest.NewRequest(http.MethodGet, "/upstream/app/v1/system/status", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated system status = %d: %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "/upstream/app/v1/system/status", nil)
	request.Header.Set("Authorization", "Bearer fixture-token")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("system status = %d: %s", response.Code, response.Body.String())
	}
	var body struct {
		Components []struct {
			Name      string  `json:"name"`
			State     string  `json:"state"`
			Detail    *string `json:"detail"`
			CheckedAt string  `json:"checkedAt"`
		} `json:"components"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Components) != 2 {
		t.Fatalf("components = %+v, want healthy and down checks; body: %s", body.Components, response.Body.String())
	}
	if got := body.Components[0]; got.Name != "healthy" || got.State != "SYSTEM_COMPONENT_STATE_OK" || got.Detail != nil {
		t.Errorf("healthy component = %+v", got)
	}
	if got := body.Components[1]; got.Name != "down" || got.State != "SYSTEM_COMPONENT_STATE_DOWN" || got.Detail == nil || !strings.Contains(*got.Detail, "fixture unavailable") {
		t.Errorf("down component = %+v", got)
	}
	for _, component := range body.Components {
		if _, err := time.Parse(time.RFC3339Nano, component.CheckedAt); err != nil {
			t.Errorf("checkedAt %q is not ProtoJSON timestamp: %v", component.CheckedAt, err)
		}
	}

	request = httptest.NewRequest(http.MethodGet, "/system/status", nil)
	request.Header.Set("Authorization", "Bearer fixture-token")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("legacy Huma system status route = %d: %s", response.Code, response.Body.String())
	}
}
