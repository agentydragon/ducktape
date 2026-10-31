package codex

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/runner/codex/protocol"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/jsonrpc"
)

// InitializeRequestID names the handshake request the Runner sends once per shared app-server.
const InitializeRequestID = "agentplane-initialize"

// ServerCommand is the command line of the Runner's shared app-server. Every option in it applies to
// every hosted Session; per-Session settings go in each thread's own parameters.
func ServerCommand(launch harness.LaunchConfig) ([]string, error) {
	catalogURL, err := launch.PlatformURL(codexCatalogPath)
	if err != nil {
		return nil, err
	}
	args := []string{launch.Binary}
	appendOverride := func(key string, value any) error {
		encoded, err := json.Marshal(value)
		if err != nil {
			return err
		}
		args = append(args, "-c", key+"="+string(encoded))
		return nil
	}
	overrides := []struct {
		key   string
		value any
	}{
		{"model_provider", "agentplane"},
		{"model_providers.agentplane.name", "Agentplane LiteLLM"},
		{"model_providers.agentplane.base_url", launch.BaseURL},
		{"model_providers.agentplane.env_key", "OPENAI_API_KEY"},
		{"model_providers.agentplane.wire_api", "responses"},
		{"model_providers.agentplane.model_catalog_url", catalogURL.String()},
		{"model_providers.agentplane.request_max_retries", 0},
		{"model_providers.agentplane.stream_max_retries", codexMaxRetries},
		{"approval_policy", "never"},
		{"sandbox_mode", "danger-full-access"},
		{"skills.bundled.enabled", false},
		{"skills.include_instructions", false},
		{"include_permissions_instructions", false},
		{"include_apps_instructions", false},
		{"include_collaboration_mode_instructions", false},
		{"include_environment_context", false},
		{"web_search", "disabled"},
		{"features.multi_agent", true},
		{"features.view_image", true},
		{"features.unbounded_connection_retries", false},
		// CLEANUP(added 2026-10-20): Remove once the pinned `agentplane_codex_cli_linux_x64` is >= 0.162,
		//   where the feature is on by default. 0.157.0 never fetches a custom provider's catalog without it.
		{"features.api_key_model_discovery", true},
	}
	for _, override := range overrides {
		if err := appendOverride(override.key, override.value); err != nil {
			return nil, err
		}
	}
	return append(args, "app-server", "--listen", "stdio://"), nil
}

// ServerEnvironment is the shared app-server's own environment: its Codex home and model credentials.
// The Runner adds it to the runner-wide harness environment.
func ServerEnvironment(launch harness.LaunchConfig, store string) map[string]string {
	return map[string]string{"CODEX_HOME": store, "OPENAI_API_KEY": launch.Token, "OPENAI_BASE_URL": launch.BaseURL}
}

// InitializeFrames are the shared app-server's handshake: the `initialize` request, named
// InitializeRequestID, and the `initialized` notification that follows its response.
func InitializeFrames() (request, notification json.RawMessage, err error) {
	request, err = json.Marshal(jsonrpc.Request[protocol.InitializeParams]{
		ID: InitializeRequestID, Method: "initialize",
		Params: protocol.InitializeParams{ClientInfo: protocol.ClientInfo{Name: codexClientName, Version: "0.1"}},
	})
	if err != nil {
		return nil, nil, err
	}
	notification, err = json.Marshal(jsonrpc.Request[any]{Method: "initialized"})
	return request, notification, err
}

// InitializeResponse reports whether `frame` answers the `initialize` request, and its refusal if it does.
func InitializeResponse(frame json.RawMessage) (bool, error) {
	response, err := jsonrpc.Parse(frame)
	if err != nil || !response.Answers(InitializeRequestID) {
		return false, nil
	}
	if err := codexResponseError(response); err != nil {
		return true, fmt.Errorf("Codex initialize failed: %w", err)
	}
	if len(response.Result) == 0 || string(response.Result) == "null" {
		return true, errors.New("Codex initialize returned no result")
	}
	return true, nil
}

// threadEnvironment is the thread configuration that gives the thread's tools the Session's own
// variables. The shared process environment is every Session's, so it cannot carry them.
func threadEnvironment(environment map[string]string) map[string]any {
	return map[string]any{"shell_environment_policy.set": environment}
}
