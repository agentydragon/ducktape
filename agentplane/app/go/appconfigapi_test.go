package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestAppConfigRoutesUseGeneratedProtoJSONAndCallerAuthentication(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: appConfigTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:tools:runner"},
	})
	if err != nil {
		t.Fatal(err)
	}

	config := Config{
		Models: ModelCatalog{
			Models: []ModelOption{{Model: "model-a", DisplayName: "Model A", ReasoningEfforts: []string{"low", "high"}}},
			Harnesses: map[string][]string{
				"HARNESS_CODEX":  {"model-a"},
				"HARNESS_CLAUDE": {},
			},
		},
		ThreadPresets: map[string]ThreadPreset{
			"default": {Harness: "HARNESS_CODEX", Model: "model-a", CWD: "/workspace", Instructions: "", SetupScript: ""},
		},
		SandboxPresets: map[string]SandboxPreset{
			"work": {Title: "Work", Template: "linux", PolicySets: []string{}, InboxTags: []string{"team"}, ThreadPreset: "default"},
		},
		sandboxPresetOrder: []string{"work"},
	}
	server := NewHTTPServer(authentication)
	if err := RegisterCoreRoutes(server, config); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(authentication)

	for _, path := range []string{"/upstream/app-config/v1/models", "/upstream/app-config/v1/presets"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusUnauthorized {
			t.Errorf("unauthenticated GET %s status = %d, want 401; body: %s", path, response.Code, response.Body.String())
		}
	}

	for _, test := range []struct {
		path string
		want string
	}{
		{
			path: "/upstream/app-config/v1/models",
			want: `{"models":[{"model":"model-a","displayName":"Model A","reasoningEfforts":["low","high"]}],"harnesses":[{"harness":"HARNESS_CLAUDE","models":[]},{"harness":"HARNESS_CODEX","models":["model-a"]}]}`,
		},
		{
			path: "/upstream/app-config/v1/presets",
			want: `{"presets":[{"name":"work","title":"Work","template":"linux","policySets":[],"inboxTags":["team"],"sessionDefaults":{"harness":"HARNESS_CODEX","model":"model-a","cwd":"/workspace","instructions":"","setupScript":""},"bootstrap":""}]}`,
		},
	} {
		request := httptest.NewRequest(http.MethodGet, test.path, nil)
		request.Header.Set("Authorization", "Bearer accepted")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusOK {
			t.Fatalf("GET %s status = %d, want 200; body: %s", test.path, response.Code, response.Body.String())
		}
		var got, want any
		if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal([]byte(test.want), &want); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("GET %s body = %v, want %v", test.path, got, want)
		}
	}

	for _, path := range []string{"/models", "/presets"} {
		oldRoute := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, path, nil)
		request.Header.Set("Authorization", "Bearer accepted")
		handler.ServeHTTP(oldRoute, request)
		if oldRoute.Code != http.StatusNotFound {
			t.Errorf("retired GET %s status = %d, want 404", path, oldRoute.Code)
		}
	}
}

type appConfigTokenReviewer struct{}

func (appConfigTokenReviewer) Create(_ context.Context, _ *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"agentplane"},
		User:          authenticationv1.UserInfo{Username: "system:serviceaccount:tools:runner"},
	}}, nil
}
