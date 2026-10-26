package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
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
	RegisterPolicyRoutes(server, Config{}, nil, nil)
	handler := server.Handler(authentication)

	for _, path := range []string{
		"/sandboxes/form",
		"/sandboxes/demo/egress",
		"/sandboxes/demo/policy-bindings",
		"/policy-bindings/demo",
	} {
		method := http.MethodGet
		if path == "/sandboxes/demo/policy-bindings" {
			method = http.MethodPost
		} else if path == "/policy-bindings/demo" {
			method = http.MethodDelete
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(method, path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", method, path, recorder.Code, recorder.Body.String())
		}
	}

	openapi := httptest.NewRecorder()
	handler.ServeHTTP(openapi, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if openapi.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d, want 200; body: %s", openapi.Code, openapi.Body.String())
	}
}

func TestSandboxFormReturnsConfiguredLifecycleDefaults(t *testing.T) {
	seconds := int64(900)
	configuration := Config{NewSandboxDefaults: NewSandboxDefaults{
		WakeOnSend: true, IdleSuspendAfterSeconds: &seconds,
	}}
	objects := runtime.NewScheme()
	if err := policyapi.AddToScheme(objects); err != nil {
		t.Fatal(err)
	}
	policies := policyaccess.New("sandboxes", fake.NewClientBuilder().WithScheme(objects).Build())
	authentication, err := auth.New(auth.Config{
		TokenReviews: policyTestTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:tools:runner"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	RegisterPolicyRoutes(server, configuration, nil, policies)
	request := httptest.NewRequest(http.MethodGet, "/sandboxes/form", nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("GET /sandboxes/form status = %d, body = %s", response.Code, response.Body.String())
	}
	var body struct {
		NewSandboxDefaults NewSandboxDefaults `json:"new_sandbox_defaults"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode Sandbox form response: %v", err)
	}
	if !body.NewSandboxDefaults.WakeOnSend || body.NewSandboxDefaults.IdleSuspendAfterSeconds == nil || *body.NewSandboxDefaults.IdleSuspendAfterSeconds != seconds {
		t.Fatalf("new Sandbox defaults = %+v", body.NewSandboxDefaults)
	}
}
