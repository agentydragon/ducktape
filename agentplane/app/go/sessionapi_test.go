package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestSessionDefaultsMergeExplicitFieldsAndHonorSetupPresence(t *testing.T) {
	config := Config{
		SandboxPresets: map[string]SandboxPreset{"work": {ThreadPreset: "default"}},
		ThreadPresets: map[string]ThreadPreset{
			"default": {
				Harness: "HARNESS_CODEX", Model: "model-a", CWD: "/state/workspaces/{session_id}",
				ReasoningEffort: sessionTestStringPtr("high"), Instructions: "from preset", SetupScript: "setup",
			},
		},
	}
	sandbox := &sandboxespb.Sandbox{Preset: proto.String("work")}
	defaults, setup, err := sessionDefaults(config, sandbox, false, nil)
	if err != nil {
		t.Fatal(err)
	}
	if defaults.GetHarness() != runnerpb.Harness_HARNESS_CODEX || defaults.GetModel() != "model-a" || defaults.GetReasoningEffort() != "high" || setup == nil || *setup != "setup" {
		t.Fatalf("unexpected preset defaults: spec=%v setup=%v", defaults, setup)
	}
	merged, err := bridge.ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "request"})
	if err != nil {
		t.Fatal(err)
	}
	merged.Cwd = replaceSessionID(merged.GetCwd(), "b351b316-fdc4-4f50-a642-758992ae3323")
	if merged.GetModel() != "" || merged.GetInstructions() != "request" || merged.GetCwd() != "/state/workspaces/b351b316-fdc4-4f50-a642-758992ae3323" {
		t.Fatalf("request did not overlay defaults: %v", merged)
	}
	if _, setup, err = sessionDefaults(config, sandbox, true, nil); err != nil || setup != nil {
		t.Fatalf("explicit null setup_script should clear preset value; setup=%v err=%v", setup, err)
	}
	requested := "alternate"
	if _, setup, err = sessionDefaults(config, sandbox, true, &requested); err != nil || setup == nil || *setup != requested {
		t.Fatalf("explicit setup_script should replace preset value; setup=%v err=%v", setup, err)
	}
	if _, _, err = sessionDefaults(config, &sandboxespb.Sandbox{Preset: proto.String("missing")}, false, nil); err == nil {
		t.Fatal("unknown Sandbox preset was accepted")
	}
}

func sessionTestStringPtr(value string) *string { return &value }

func TestSessionRoutesRequireCallerAuthentication(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	RegisterSessionRoutes(server, Config{}, nil, nil, nil, false)
	handler := server.Handler(authentication)
	for _, test := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/sandboxes/demo/sessions"},
		{http.MethodGet, "/sandboxes/demo/sessions/" + uuid.NewString() + "/opening"},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(test.method, test.path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", test.method, test.path, recorder.Code, recorder.Body.String())
		}
	}
}

var (
	_ sessionOpener = (*bridge.RunnerBridge)(nil)
	_ threadOpener  = (*threads.Store)(nil)
)

func TestNewSessionBodyTracksAnExplicitNullSetupScript(t *testing.T) {
	var omitted, cleared newSessionBody
	if err := json.Unmarshal([]byte(`{"session_id":"00000000-0000-0000-0000-000000000000","spec":{}}`), &omitted); err != nil {
		t.Fatal(err)
	}
	if omitted.SetupScriptPresent {
		t.Fatal("omitted setup_script was marked present")
	}
	if err := json.Unmarshal([]byte(`{"session_id":"00000000-0000-0000-0000-000000000000","spec":{},"setup_script":null}`), &cleared); err != nil {
		t.Fatal(err)
	}
	if !cleared.SetupScriptPresent || cleared.SetupScript != nil {
		t.Fatalf("explicit null setup_script was not retained: %+v", cleared)
	}
}
