package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type policyTestTokenReviewer struct{}

func (policyTestTokenReviewer) Create(_ context.Context, _ *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}, nil
}
func TestPolicyRoutesArePublishedAndCallerProtected(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	RegisterStreamingRoutes(server, StreamingDependencies{})
	handler := server.Handler(authentication)

	for _, request := range []struct{ method, path string }{
		{http.MethodPost, "/upstream/policy/v1/sandboxes/policies/demo/policy-bindings"},
		{http.MethodDelete, "/upstream/policy/v1/sandboxes/policies/demo/policy-bindings/grant?sandboxUid=sandbox-uid&bindingUid=binding-uid"},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(request.method, request.path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", request.method, request.path, recorder.Code, recorder.Body.String())
		}
	}

	openapi := httptest.NewRecorder()
	handler.ServeHTTP(openapi, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if openapi.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d, want 200; body: %s", openapi.Code, openapi.Body.String())
	}
}
