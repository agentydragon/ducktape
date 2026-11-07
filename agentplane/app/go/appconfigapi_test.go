package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/emptypb"
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
		Models: runtimeconfig.ModelCatalog{
			Models: []runtimeconfig.ModelOption{{Model: "model-a", DisplayName: "Model A", ReasoningEfforts: []string{"low", "high"}}},
			Harnesses: map[string][]string{
				"HARNESS_CODEX":  {"model-a"},
				"HARNESS_CLAUDE": {},
			},
		},
		FrontendConfig: FrontendConfig{
			TraceSearch: &TraceSearchSettings{
				GrafanaBaseURL: "https://grafana.example.test/", TempoDatasourceUID: "fixture-tempo", DeploymentEnvironment: "fixture",
			},
			LangfuseSearch: &LangfuseSearchSettings{
				BaseURL: "https://langfuse.example.test/", ProjectID: "fixture-project",
				SessionMetadataKey: "session", SandboxNamespaceMetadataKey: "namespace",
				SandboxServiceAccountMetadataKey: "account",
			},
		},
		ThreadPresets: map[string]ThreadPreset{
			"default": {Harness: "HARNESS_CODEX", Model: "model-a", CWD: "/workspace", Instructions: "", SetupScript: ""},
		},
		SandboxPresets: map[string]SandboxPreset{
			"work": {Title: "Work", Template: "linux", PolicySets: []string{}, InboxTags: []string{"team"}, ThreadPreset: "default"},
		},
		sandboxPresetOrder: []string{"work"},
		NewSandboxDefaults: NewSandboxDefaults{WakeOnSend: true, IdleSuspendAfterSeconds: appConfigInt64Pointer(900)},
	}
	server := NewHTTPServer(authentication)
	policySets := &appConfigPolicySetNames{names: []string{"alpha", "zeta"}}
	if err := RegisterAppConfigRoutes(server, &appService{config: config, policySetNames: policySets}); err != nil {
		t.Fatal(err)
	}
	handler := server.Handler(authentication)

	for _, path := range []string{
		"/upstream/app-config/v1/models",
		"/upstream/app-config/v1/presets",
		"/upstream/app-config/v1/frontend-config",
		"/upstream/app-config/v1/sandbox-form",
	} {
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
		{
			path: "/upstream/app-config/v1/frontend-config",
			want: `{"frontendConfig":{"traceSearch":{"grafanaBaseUrl":"https://grafana.example.test/","tempoDatasourceUid":"fixture-tempo","deploymentEnvironment":"fixture"},"langfuseSearch":{"baseUrl":"https://langfuse.example.test/","projectId":"fixture-project","sessionMetadataKey":"session","sandboxNamespaceMetadataKey":"namespace","sandboxServiceAccountMetadataKey":"account"}}}`,
		},
		{
			path: "/upstream/app-config/v1/sandbox-form",
			want: `{"policySets":["alpha","zeta"],"newSandboxDefaults":{"wakeOnSend":true,"idleSuspendAfterSeconds":"900"}}`,
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
	if policySets.calls != 1 {
		t.Fatalf("PolicyService name-list calls = %d, want one per form request", policySets.calls)
	}
}

type appConfigPolicySetNames struct {
	calls int
	names []string
	err   error
}

func (r *appConfigPolicySetNames) ListPolicySetNames(context.Context, *emptypb.Empty, ...grpc.CallOption) (*policypb.ListPolicySetNamesResponse, error) {
	r.calls++
	if r.err != nil {
		return nil, r.err
	}
	return &policypb.ListPolicySetNamesResponse{Names: r.names}, nil
}

func appConfigInt64Pointer(value int64) *int64 { return &value }

func TestSandboxFormReturnsUnavailableWhenPolicyServiceFails(t *testing.T) {
	authentication, err := auth.New(auth.Config{
		TokenReviews: appConfigTokenReviewer{}, TokenAudience: "agentplane",
		TokenSubjects: []string{"system:serviceaccount:tools:runner"},
	})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	policySets := &appConfigPolicySetNames{err: errors.New("policy backend unavailable")}
	if err := RegisterAppConfigRoutes(server, &appService{policySetNames: policySets}); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/upstream/app-config/v1/sandbox-form", nil)
	request.Header.Set("Authorization", "Bearer accepted")
	response := httptest.NewRecorder()
	server.Handler(authentication).ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("GET sandbox form status = %d, want 503; body: %s", response.Code, response.Body.String())
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
